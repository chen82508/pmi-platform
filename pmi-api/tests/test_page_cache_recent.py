"""page_cache.scalar_points SQL shape + board_history_points (no DB).

board_history_points reads the slim seat-board history and falls back to the
full board rows only for the part of a window the slim series does not reach.
Ending-now reads are memoized because one seat page fans out five concurrent
API calls and three of them ask for the same series.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from pmi_api import page_cache
from pmi_api.db_guard import QueryTimeout

SLIM = "senate-board-trend:x"
FULL = "senate-board:x"


def _cache_key(stmt) -> str:
    params = stmt.compile(dialect=postgresql.dialect()).params
    return next(v for v in params.values() if isinstance(v, str) and v.startswith("senate-board"))


class _Session:
    """Answers scalar_points (execute) per cache key and the slim-coverage
    probe (scalar). Each call yields first so concurrent callers overlap."""

    def __init__(self, rows: dict[str, list] | None = None, slim_start: datetime | None = None):
        self.rows = rows if rows is not None else {SLIM: [(datetime(2026, 10, 20, 12, tzinfo=UTC), "51.5", "14")]}
        self.slim_start = slim_start if slim_start is not None else datetime(2000, 1, 1, tzinfo=UTC)
        self.executed: list[str] = []
        self.probes = 0

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield

    async def execute(self, stmt):
        if isinstance(stmt, TextClause):  # db_guard's SET LOCAL statement_timeout
            return None
        key = _cache_key(stmt)
        self.executed.append(key)
        await asyncio.sleep(0.01)
        rows = self.rows.get(key, [])

        class _R:
            def all(self_inner):
                return rows

        return _R()

    async def scalar(self, stmt):
        self.probes += 1
        await asyncio.sleep(0)
        return self.slim_start


@pytest.fixture(autouse=True)
def _clear_memo():
    page_cache._recent.clear()
    page_cache._recent_locks.clear()
    yield
    page_cache._recent.clear()
    page_cache._recent_locks.clear()


def test_scalar_points_extracts_once_in_materialized_cte():
    captured = []

    class _S:
        @contextlib.asynccontextmanager
        async def begin_nested(self):
            yield

        async def execute(self, stmt):
            if isinstance(stmt, TextClause):
                return None
            captured.append(stmt)

            class _R:
                def all(self_inner):
                    return []

            return _R()

    asyncio.run(
        page_cache.scalar_points(_S(), FULL, ("p_r_control",), datetime.now(UTC), 14, bucket="hour")
    )
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "AS MATERIALIZED" in sql
    # The JSONB path is extracted only inside the CTE, never in the outer filter.
    assert sql.count("#>>") == 1


def test_reads_only_the_slim_series_when_it_covers_the_window():
    session = _Session()
    rows = asyncio.run(page_cache.board_history_points(session, "x", ("p_r_control",), 14))
    assert rows == [("2026-10-20T12:00:00+00:00", 51.5, 14)]
    assert session.executed == [SLIM]


def test_concurrent_misses_share_one_query_and_are_memoized():
    session = _Session()

    async def main():
        results = await asyncio.gather(
            *[page_cache.board_history_points(session, "x", ("p_r_control",), 14) for _ in range(5)]
        )
        again = await page_cache.board_history_points(session, "x", ("p_r_control",), 14)
        return results, again

    results, again = asyncio.run(main())
    assert session.executed == [SLIM]
    assert session.probes == 1
    assert all(r == results[0] for r in results) and again == results[0]


def test_memo_is_per_path_and_expires(monkeypatch):
    session = _Session()
    clock = [1000.0]
    monkeypatch.setattr(page_cache, "_clock", lambda: clock[0])

    def call(path):
        return asyncio.run(page_cache.board_history_points(session, "x", path, 30))

    call(("prob_by_state", "TX"))
    call(("prob_by_state", "OH"))
    assert len(session.executed) == 2  # distinct seats are distinct entries
    call(("prob_by_state", "TX"))
    assert len(session.executed) == 2  # still fresh
    clock[0] += page_cache._RECENT_TTL_SEC + 1
    call(("prob_by_state", "TX"))
    assert len(session.executed) == 3  # expired → re-queried


def test_as_of_reads_bypass_the_memo():
    session = _Session()
    as_of = datetime(2026, 10, 1, tzinfo=UTC)
    for _ in range(2):
        asyncio.run(page_cache.board_history_points(session, "x", ("p_r_control",), 14, as_of=as_of))
    assert session.executed == [SLIM, SLIM]


def test_falls_back_to_full_rows_only_for_the_uncovered_gap():
    now = datetime.now(UTC)
    slim_start = (now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    old_hour = (now - timedelta(days=5)).replace(minute=0, second=0, microsecond=0)
    session = _Session(
        rows={
            SLIM: [(slim_start, "60.0", "15")],
            # the full key repeats the slim series' first bucket; slim wins
            FULL: [(old_hour, "40.0", "14"), (slim_start, "99.0", "15")],
        },
        slim_start=slim_start,
    )
    rows = asyncio.run(page_cache.board_history_points(session, "x", ("p_r_control",), 30))
    assert session.executed == [SLIM, FULL]
    assert rows == [(old_hour.isoformat(), 40.0, 14), (slim_start.isoformat(), 60.0, 15)]


def test_unknown_seat_does_not_trigger_the_full_scan():
    session = _Session(rows={SLIM: []})  # slim key covers the window, seat has no values
    rows = asyncio.run(page_cache.board_history_points(session, "x", ("prob_by_district", "ZZ-99"), 90))
    assert rows == []
    assert session.executed == [SLIM]


def test_paths_outside_the_slim_payload_are_rejected():
    with pytest.raises(ValueError):
        asyncio.run(page_cache.board_history_points(_Session(), "x", ("races",), 14))


def test_board_history_timeout_returns_empty_and_is_not_memoized(monkeypatch):
    session = _Session()
    calls = {"n": 0}
    real = page_cache._trend_points

    async def slow_then_ok(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise QueryTimeout("canceling statement due to statement timeout")
        return await real(*args, **kwargs)

    monkeypatch.setattr(page_cache, "_trend_points", slow_then_ok)
    first = asyncio.run(page_cache.board_history_points(session, "x", ("p_r_control",), 14))
    second = asyncio.run(page_cache.board_history_points(session, "x", ("p_r_control",), 14))
    assert first == []  # chart dropped, request still succeeds
    assert second == [("2026-10-20T12:00:00+00:00", 51.5, 14)]  # retried, not served from memo
    assert calls["n"] == 2


def test_scalar_points_timeout_returns_empty(monkeypatch):
    async def timed_out(*args, **kwargs):
        raise QueryTimeout("canceling statement due to statement timeout")

    monkeypatch.setattr(page_cache, "_scalar_points_query", timed_out)
    rows = asyncio.run(
        page_cache.scalar_points(_Session(), "gdp-board:us-gdp-expectation", ("score",), datetime.now(UTC), 90, bucket="hour")
    )
    assert rows == []
