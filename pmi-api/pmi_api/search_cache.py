"""Stale-while-revalidate cache for /markets/search: in-process, optionally
backed by Redis.

A cold search on the data box takes 17–60 s: the title trigram index is
2.4 GB, a query touches ~10k of its pages, and on a saturated disk most of
them come off disk (measured 2026-10-21/22). Nearly all search traffic is our
own scheduled script, which repeats the same ~9–18 terms in bursts 37–90
minutes apart, so caching per term removes almost every repeat:

* younger than ``fresh_s``: served from memory.
* older than ``fresh_s`` but younger than ``stale_s``: served at once, and one
  background query (own session, bounded, limited concurrency) refreshes it.
* older than ``stale_s`` or never seen: queried on the request. Concurrent
  requests for the same key in this process wait for the first one (single
  flight), with the wait bounded by the same budget as the query.

Tier 1 is a dict in this process (fastest, survives a Redis outage). Tier 2,
when ``redis_url`` is set, is a Redis shared by every API process: it survives
pmi-api restarts and deploys, which otherwise start every term cold. Redis is
strictly optional: each call has a short timeout, and on any error the cache
skips Redis for a cool-down period and keeps working from tier 1 / the DB.

A query or wait past ``query_timeout_s`` raises ``SearchBusy``; the route turns
that into a 503 with Retry-After. Failures are never cached. Values must be
JSON-serializable (the route caches plain dicts).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.db_guard import QueryTimeout, bounded

log = structlog.get_logger(__name__)

Run = Callable[[AsyncSession], Awaitable[Any]]
SessionFactory = Callable[[], Any]  # returns an AsyncSession async context manager


class SearchBusy(Exception):
    """The query (or the wait for an identical in-flight query) ran out of time."""


@dataclass
class _Entry:
    value: Any
    stored_at: float  # wall-clock seconds, comparable across processes


def _default_session_factory() -> Any:
    from pmi_core.db import SessionLocal  # imported lazily: tests inject their own

    return SessionLocal()


class RedisTier:
    """Tier 2. Every operation is time-bounded and fail-open."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_s: int,
        prefix: str = "pmi:search:v1:",
        op_timeout_s: float = 0.25,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.ttl_s = max(1, int(ttl_s))
        self.prefix = prefix
        self.op_timeout_s = op_timeout_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._down_until = 0.0

    @classmethod
    def from_url(cls, url: str, **kw: Any) -> RedisTier:
        import redis.asyncio as aioredis

        timeout = kw.get("op_timeout_s", 0.25)
        client = aioredis.from_url(
            url, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
        )
        return cls(client, **kw)

    def _rkey(self, key: Hashable) -> str:
        return self.prefix + json.dumps(list(key) if isinstance(key, tuple) else key)

    @property
    def available(self) -> bool:
        return self._clock() >= self._down_until

    def _fail(self, op: str, exc: BaseException) -> None:
        if self.available:  # log once per outage, not once per request
            log.warning("api.search_cache.redis_unavailable", op=op, error=repr(exc),
                        retry_in_s=self.cooldown_s)
        self._down_until = self._clock() + self.cooldown_s

    async def get(self, key: Hashable) -> _Entry | None:
        if not self.available:
            return None
        try:
            raw = await asyncio.wait_for(self._client.get(self._rkey(key)), self.op_timeout_s)
        except Exception as exc:
            self._fail("get", exc)
            return None
        if raw is None:
            return None
        try:
            doc = json.loads(raw)
            return _Entry(value=doc["v"], stored_at=float(doc["t"]))
        except (ValueError, KeyError, TypeError):
            return None  # unreadable entry: treat as a miss; the next store overwrites it

    async def set(self, key: Hashable, entry: _Entry) -> None:
        if not self.available:
            return
        payload = json.dumps({"v": entry.value, "t": entry.stored_at})
        try:
            await asyncio.wait_for(
                self._client.set(self._rkey(key), payload, ex=self.ttl_s), self.op_timeout_s
            )
        except Exception as exc:
            self._fail("set", exc)


class SearchCache:
    def __init__(
        self,
        *,
        fresh_s: float,
        stale_s: float,
        max_entries: int,
        query_timeout_s: float,
        refresh_concurrency: int = 2,
        l2: RedisTier | None = None,
        session_factory: SessionFactory = _default_session_factory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.fresh_s = fresh_s
        self.stale_s = max(stale_s, fresh_s)
        self.max_entries = max_entries
        self.query_timeout_s = query_timeout_s
        self.refresh_concurrency = max(1, refresh_concurrency)
        self.l2 = l2
        self._session_factory = session_factory
        self._clock = clock
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._refreshing: set[Hashable] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._sem: asyncio.Semaphore | None = None

    @property
    def enabled(self) -> bool:
        return self.fresh_s > 0 and self.max_entries > 0

    def clear(self) -> None:
        """Empty tier 1 (tier 2 entries expire on their own TTL)."""
        self._entries.clear()
        self._locks.clear()
        self._refreshing.clear()

    async def get(self, key: Hashable, run: Run, session: AsyncSession) -> tuple[Any, str]:
        """Return ``(value, status)``.

        status: hit / stale (tier 1), l2-hit / l2-stale (tier 2, e.g. after a
        restart), miss (queried on the request), bypass (cache disabled).
        """
        if not self.enabled:
            return await self._query(session, run), "bypass"

        served = await self._serve_cached(key, run)
        if served is not None:
            return served

        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.query_timeout_s)
        except TimeoutError as exc:
            raise SearchBusy("waited too long for an identical search") from exc
        try:
            entry = self._lookup(key)  # the request we waited on may have filled it
            if entry is not None and self._age(entry) < self.fresh_s:
                return entry.value, "hit"
            value = await self._query(session, run)
            await self._store(key, value)
            return value, "miss"
        finally:
            lock.release()

    async def drain(self) -> None:
        """Wait for background refreshes (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- internals ---------------------------------------------------------

    async def _serve_cached(self, key: Hashable, run: Run) -> tuple[Any, str] | None:
        entry, tier = self._lookup(key), ""
        if entry is None and self.l2 is not None:
            entry = await self.l2.get(key)
            if entry is not None:
                tier = "l2-"
                self._store_local(key, entry)  # keep its original age
        if entry is None:
            return None
        age = self._age(entry)
        if age < self.fresh_s:
            return entry.value, f"{tier}hit"
        if age < self.stale_s:
            self._schedule_refresh(key, run)
            return entry.value, f"{tier}stale"
        return None

    def _age(self, entry: _Entry) -> float:
        return self._clock() - entry.stored_at

    async def _query(self, session: AsyncSession, run: Run) -> Any:
        try:
            return await bounded(
                session, int(self.query_timeout_s * 1000), lambda: run(session)
            )
        except QueryTimeout as exc:
            raise SearchBusy(str(exc)) from exc

    def _lookup(self, key: Hashable) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def _store_local(self, key: Hashable, entry: _Entry) -> None:
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            old, _ = self._entries.popitem(last=False)
            self._locks.pop(old, None)

    async def _store(self, key: Hashable, value: Any) -> None:
        entry = _Entry(value=value, stored_at=self._clock())
        self._store_local(key, entry)
        if self.l2 is not None:
            await self.l2.set(key, entry)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.refresh_concurrency)
        return self._sem

    def _schedule_refresh(self, key: Hashable, run: Run) -> None:
        if key in self._refreshing:
            return
        self._refreshing.add(key)
        task = asyncio.create_task(self._refresh(key, run))
        self._tasks.add(task)  # keep a strong reference until it finishes
        task.add_done_callback(self._tasks.discard)

    async def _refresh(self, key: Hashable, run: Run) -> None:
        started = time.monotonic()
        try:
            async with self._semaphore():
                async with self._session_factory() as session:
                    value = await self._query(session, run)
            await self._store(key, value)
            log.info("api.search_cache.refreshed", key=repr(key),
                     seconds=round(time.monotonic() - started, 2))
        except Exception as exc:  # the stale entry stays; the next stale hit retries
            log.warning("api.search_cache.refresh_failed", key=repr(key), error=str(exc))
        finally:
            self._refreshing.discard(key)
