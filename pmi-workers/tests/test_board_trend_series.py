"""Seat-board slim history: writer, control series reader, and the generic
trend backfill run against the test Postgres."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pmi_core.engine.board_trend import slim_payload
from pmi_core.models import TsPageCache
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql

from pmi_workers.jobs import _board_series, board_trend_backfill

FULL = "senate-board:us-house-2026-republican-seats"
SLIM = "senate-board-trend:us-house-2026-republican-seats"


def _params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


class _Session:
    def __init__(self, rows: dict[str, list]):
        self.rows = rows
        self.read: list[str] = []
        self.inserted: list[dict] = []

    async def execute(self, stmt):
        params = _params(stmt)
        if stmt.is_insert:
            self.inserted.append(params)
            return None
        key = next(v for v in params.values() if isinstance(v, str) and v.startswith("senate-board"))
        self.read.append(key)
        rows = self.rows.get(key, [])

        class _R:
            def all(self_inner):
                return rows

        return _R()


def test_write_board_trend_inserts_the_slim_row():
    session = _Session({})
    at = datetime(2026, 10, 20, 14, tzinfo=UTC)
    payload = {"p_r_control": 1.0, "prob_by_district": {"NJ-12": 0.2}, "version": 9, "races": [1, 2]}
    asyncio.run(_board_series.write_board_trend(session, FULL, payload, at))
    [row] = session.inserted
    assert row["cache_key"] == SLIM
    assert row["payload"] == {"p_r_control": 1.0, "prob_by_district": {"NJ-12": 0.2}, "version": 9}
    assert row["computed_at"] == at


def test_control_series_reads_slim_only_when_it_covers_the_window():
    now = datetime(2026, 10, 20, 14, tzinfo=UTC)
    slim_rows = [(now - timedelta(days=d), 50.0 + d) for d in range(14, 0, -1)]
    session = _Session({SLIM: slim_rows})
    series = asyncio.run(_board_series.control_prob_series(session, FULL, now, 42.0))
    assert session.read == [SLIM]
    assert series[-1] == 42.0 and len(series) == 14


def test_control_series_fills_the_gap_from_full_rows():
    now = datetime(2026, 10, 20, 14, tzinfo=UTC)
    session = _Session(
        {
            SLIM: [(now - timedelta(days=2), 60.0)],
            FULL: [(now - timedelta(days=10), 40.0)],
        }
    )
    series = asyncio.run(_board_series.control_prob_series(session, FULL, now, 42.0))
    assert session.read == [SLIM, FULL]
    assert series == [40.0, 60.0, 42.0]


# ── board-trend-backfill against a real Postgres (the test DB) ──────────────
# The generic backfill streams full rows and projects them in Python
# (trend_store); this runs it end to end against the test DB.


@pytest_asyncio.fixture
async def _empty_page_cache(session_factory):
    async with session_factory() as s:
        await s.execute(text("TRUNCATE ts_page_cache RESTART IDENTITY"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("TRUNCATE ts_page_cache RESTART IDENTITY"))
        await s.commit()


@pytest.mark.asyncio
async def test_backfill_rebuilds_the_window_from_full_rows(session_factory, _empty_page_cache):
    t0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
    full = [
        {"p_r_control": 41.0, "prob_by_district": {"NJ-12": 0.1}, "version": 9, "races": [1]},
        {"p_r_control": 42.0, "prob_by_district": {"NJ-12": None}, "version": 9, "races": [2]},
        {"p_r_control": 43.0, "version": 8, "races": [3]},  # older row, no district map
    ]
    async with session_factory() as s:
        for i, payload in enumerate(full):
            s.add(TsPageCache(cache_key=FULL, payload=payload, computed_at=t0 + timedelta(hours=i)))
        # A pre-existing (possibly narrower) slim row: the rebuild replaces it.
        s.add(TsPageCache(cache_key=SLIM, payload=slim_payload(full[0]), computed_at=t0))
        await s.commit()

    # days huge so the backfill window is clock-independent in CI.
    first = await board_trend_backfill.run(days=36500)
    # rebuilt is keyed by the full source cache key: the whole window is rebuilt.
    assert first["rebuilt"] == {FULL: 3}

    async with session_factory() as s:
        rows = (
            await s.execute(
                select(TsPageCache.computed_at, TsPageCache.payload)
                .where(TsPageCache.cache_key == SLIM)
                .order_by(TsPageCache.computed_at)
            )
        ).all()
    assert [r.computed_at for r in rows] == [t0 + timedelta(hours=i) for i in range(3)]
    # Python projection keeps nested nulls (same as the forward-written slim row);
    # readers extract with #>>, which maps a JSON null and a missing key alike.
    assert rows[1].payload == {"p_r_control": 42.0, "prob_by_district": {"NJ-12": None}, "version": 9}
    assert rows[2].payload == {"p_r_control": 43.0, "version": 8}
    assert all("races" not in r.payload for r in rows)

    # Idempotent in final state: a second run reproduces the identical slim rows.
    second = await board_trend_backfill.run(days=36500)
    assert second["rebuilt"] == {FULL: 3}
    async with session_factory() as s:
        count = await s.scalar(
            select(func.count()).select_from(TsPageCache).where(TsPageCache.cache_key == SLIM)
        )
    assert count == 3
