"""SearchCache policy (no DB): fresh hit, stale-while-revalidate, single
flight, timeouts, failure handling, eviction, and the optional Redis tier."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy.sql.elements import TextClause

from pmi_api.db_guard import QueryTimeout
from pmi_api.search_cache import RedisTier, SearchBusy, SearchCache

pytestmark = pytest.mark.asyncio


class _Session:
    """Enough of AsyncSession for db_guard.bounded: a savepoint and SET LOCAL."""

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield

    async def execute(self, stmt):
        assert isinstance(stmt, TextClause)  # only db_guard's SET LOCAL reaches here


class _Factory:
    """Session factory for background refreshes; counts sessions opened."""

    def __init__(self) -> None:
        self.opened = 0

    @contextlib.asynccontextmanager
    async def _cm(self):
        self.opened += 1
        yield _Session()

    def __call__(self):
        return self._cm()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cache(clock=None, factory=None, **kw) -> SearchCache:
    params = dict(fresh_s=600, stale_s=7200, max_entries=8, query_timeout_s=5)
    params.update(kw)
    return SearchCache(
        **params, session_factory=factory or _Factory(), clock=clock or _Clock()
    )


def _counting_run(values):
    calls = {"n": 0}

    async def run(_session):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return values[min(calls["n"], len(values)) - 1]

    return run, calls


async def test_miss_then_fresh_hit_queries_once():
    cache = _cache()
    run, calls = _counting_run(["rows-1"])
    assert await cache.get("k", run, _Session()) == ("rows-1", "miss")
    assert await cache.get("k", run, _Session()) == ("rows-1", "hit")
    assert calls["n"] == 1


async def test_stale_is_served_at_once_and_refreshed_in_the_background():
    clock, factory = _Clock(), _Factory()
    cache = _cache(clock=clock, factory=factory)
    run, calls = _counting_run(["old", "new"])
    await cache.get("k", run, _Session())

    clock.now += 601  # past fresh, within stale
    assert await cache.get("k", run, _Session()) == ("old", "stale")
    # A second stale hit while the refresh is running does not start another.
    assert await cache.get("k", run, _Session()) == ("old", "stale")
    await cache.drain()
    assert calls["n"] == 2 and factory.opened == 1  # refresh used its own session
    assert await cache.get("k", run, _Session()) == ("new", "hit")


async def test_entry_past_stale_window_is_queried_on_the_request():
    clock = _Clock()
    cache = _cache(clock=clock)
    run, calls = _counting_run(["old", "new"])
    await cache.get("k", run, _Session())
    clock.now += 7201
    assert await cache.get("k", run, _Session()) == ("new", "miss")
    assert calls["n"] == 2


async def test_concurrent_misses_share_one_query():
    cache = _cache()
    run, calls = _counting_run(["rows"])
    results = await asyncio.gather(*[cache.get("k", run, _Session()) for _ in range(5)])
    assert calls["n"] == 1
    assert sorted(status for _, status in results) == ["hit"] * 4 + ["miss"]
    assert all(value == "rows" for value, _ in results)


async def test_query_timeout_raises_busy_and_is_not_cached():
    cache = _cache()
    attempts = {"n": 0}

    async def run(_session):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise QueryTimeout("canceling statement due to statement timeout")
        return "rows"

    with pytest.raises(SearchBusy):
        await cache.get("k", run, _Session())
    assert await cache.get("k", run, _Session()) == ("rows", "miss")


async def test_waiting_for_an_identical_query_is_bounded():
    cache = _cache(query_timeout_s=0.05)
    started = asyncio.Event()

    async def slow(_session):
        started.set()
        await asyncio.sleep(0.5)
        return "rows"

    leader = asyncio.create_task(cache.get("k", slow, _Session()))
    await started.wait()
    with pytest.raises(SearchBusy):
        await cache.get("k", slow, _Session())
    assert (await leader)[1] == "miss"


async def test_failed_refresh_keeps_the_stale_entry():
    clock = _Clock()
    cache = _cache(clock=clock)
    calls = {"n": 0}

    async def run(_session):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db went away")
        return "old"

    await cache.get("k", run, _Session())
    clock.now += 601
    assert await cache.get("k", run, _Session()) == ("old", "stale")
    await cache.drain()  # refresh failed; logged, not raised
    assert await cache.get("k", run, _Session()) == ("old", "stale")
    await cache.drain()


async def test_least_recently_used_entries_are_evicted():
    cache = _cache(max_entries=2)
    run, calls = _counting_run(["a", "b", "c", "a2"])
    await cache.get("a", run, _Session())
    await cache.get("b", run, _Session())
    await cache.get("c", run, _Session())  # evicts "a"
    assert (await cache.get("a", run, _Session()))[1] == "miss"
    assert calls["n"] == 4


async def test_disabled_cache_always_queries():
    cache = _cache(fresh_s=0)
    run, calls = _counting_run(["x", "y"])
    assert await cache.get("k", run, _Session()) == ("x", "bypass")
    assert await cache.get("k", run, _Session()) == ("y", "bypass")
    assert calls["n"] == 2


# -- tier 2 (Redis) -----------------------------------------------------------


class _FakeRedis:
    """The two redis.asyncio calls RedisTier uses, backed by a dict."""

    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value
        self.ttl[key] = ex


def _tier(redis, mono=None) -> RedisTier:
    return RedisTier(redis, ttl_s=7200, clock=mono or _Clock())


async def test_store_writes_through_to_redis_with_ttl():
    redis = _FakeRedis()
    cache = _cache(l2=_tier(redis))
    run, _ = _counting_run([[{"title": "a"}]])
    await cache.get(("abbott", 50, False), run, _Session())
    (rkey,) = redis.data
    assert rkey == 'pmi:search:v1:["abbott", 50, false]'
    assert redis.ttl[rkey] == 7200


async def test_restart_is_served_from_redis_without_querying():
    redis, clock = _FakeRedis(), _Clock()
    run, calls = _counting_run(["rows"])
    await _cache(clock=clock, l2=_tier(redis)).get("k", run, _Session())

    restarted = _cache(clock=clock, l2=_tier(redis))  # empty tier 1, same Redis
    assert await restarted.get("k", run, _Session()) == ("rows", "l2-hit")
    assert await restarted.get("k", run, _Session()) == ("rows", "hit")  # now in tier 1
    assert calls["n"] == 1


async def test_stale_redis_entry_after_restart_is_served_and_refreshed():
    redis, clock = _FakeRedis(), _Clock()
    run, calls = _counting_run(["old", "new"])
    await _cache(clock=clock, l2=_tier(redis)).get("k", run, _Session())

    clock.now += 601  # past fresh, within stale
    restarted = _cache(clock=clock, l2=_tier(redis))
    assert await restarted.get("k", run, _Session()) == ("old", "l2-stale")
    await restarted.drain()
    assert calls["n"] == 2
    fresh_restart = _cache(clock=clock, l2=_tier(redis))  # refresh wrote back to Redis
    assert await fresh_restart.get("k", run, _Session()) == ("new", "l2-hit")


async def test_redis_outage_falls_back_to_the_database_and_cools_down():
    redis, mono = _FakeRedis(fail=True), _Clock()
    cache = _cache(l2=_tier(redis, mono))
    run, calls = _counting_run(["rows", "rows2"])
    assert await cache.get("k", run, _Session()) == ("rows", "miss")
    assert redis.calls == 1  # the get failed; the set was skipped during cool-down
    cache.clear()
    assert await cache.get("k", run, _Session()) == ("rows2", "miss")
    assert redis.calls == 1  # still cooling down: Redis not even tried

    mono.now += 31  # cool-down over
    redis.fail = False
    cache.clear()
    await cache.get("k", run, _Session())
    assert redis.calls == 3  # tried again: get (miss) + set


async def test_unreadable_redis_entry_is_treated_as_a_miss():
    redis = _FakeRedis()
    tier = _tier(redis)
    redis.data[tier._rkey("k")] = "not json"
    run, calls = _counting_run(["rows"])
    assert await _cache(l2=tier).get("k", run, _Session()) == ("rows", "miss")
    assert calls["n"] == 1
