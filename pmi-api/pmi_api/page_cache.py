"""Shared ts_page_cache readers for the cache-backed vertical routers.

Three access patterns:

* ``read_latest`` — newest payload for a key, optionally as of a historical
  instant. ts_page_cache is append-only, so honoring ``as_of`` is just an
  upper bound on ``computed_at`` — without it the endpoints stamped today's
  cache with the caller's historical timestamp.
* ``scalar_points`` — one (date, value, version) per bucket for a JSONB path,
  extracted in SQL (``DISTINCT ON`` + ``#>>``) rather than pulling each full
  payload into Python. The low-level reader, bounded by
  ``PMI_API_HISTORY_TIMEOUT_MS`` (a slow disk degrades to an empty series, not
  a 500); it still detoasts whichever key it is pointed at, so callers point it
  at the slim trend key, not the full board.
* ``trend_series`` — the trend reader every vertical uses: the slim companion
  key (``pmi_core.engine.trend_store``) with a fall back to the full key only
  for the part of the window the slim series has not reached, a 90-day cap, one
  time budget over the whole read, and a 60 s memo on the ending-now case.
  ``board_history_points`` is the seat-board wrapper over it.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.engine import trend_store
from pmi_core.engine.board_trend import board_key
from pmi_core.models import TsPageCache
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.config import api_settings
from pmi_api.db_guard import QueryTimeout, bounded

log = structlog.get_logger(__name__)

Points = list[tuple[str, float, int | None]]


async def read_latest(
    session: AsyncSession, cache_key: str, as_of: datetime | None = None
) -> dict | None:
    """Latest payload for ``cache_key`` (computed at or before ``as_of``),
    or None if nothing has been computed yet / existed at that time."""
    stmt = (
        select(TsPageCache.payload)
        .where(TsPageCache.cache_key == cache_key)
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )
    if as_of is not None:
        stmt = stmt.where(TsPageCache.computed_at <= as_of)
    return await session.scalar(stmt)


async def scalar_points(
    session: AsyncSession,
    cache_key: str,
    path: tuple[str, ...],
    cutoff: datetime,
    days: int,
    bucket: str = "day",
) -> Points:
    """History series for one JSONB path of ``cache_key`` (see
    ``_scalar_points_query``), bounded by ``PMI_API_HISTORY_TIMEOUT_MS``.

    A history is an optional part of its response (a trend line), so when the
    read runs past its budget it is dropped (empty series, warning logged)
    instead of failing the whole request. 2026-10-20: a saturated data-box disk
    pushed a 1,704-row GDP history past the 30 s statement timeout and
    /gdp/board returned 500.
    """
    try:
        return await bounded(
            session,
            api_settings.history_timeout_ms,
            lambda: _scalar_points_query(session, cache_key, path, cutoff, days, bucket),
        )
    except QueryTimeout:
        log.warning("api.history_timeout", cache_key=cache_key, path=list(path), days=days)
        return []


async def _scalar_points_query(
    session: AsyncSession,
    cache_key: str,
    path: tuple[str, ...],
    cutoff: datetime,
    days: int,
    bucket: str = "day",
) -> Points:
    """[(iso_time, value, methodology_version), …] ascending by time.

    One point per ``bucket`` ("day" or "hour"): the bucket's last snapshot whose
    ``payload #>> path`` is non-null. Snapshots land every ~10 min but the
    underlying score moves hourly, so "hour" is the finest meaningful bucket and
    "day" thins it further for a cleaner multi-week view. ``version`` comes from
    the payload's top-level ``version`` field when present (older rows → None).
    """
    window_start = cutoff - timedelta(days=days)
    # Extract inside a MATERIALIZED CTE so each row's TOASTed payload is
    # decompressed for the SELECT list only; filtering `value IS NOT NULL` in
    # the same query level made Postgres decompress it again. House board rows
    # are ~150 kB of JSON each — measured 830 → 510 ms on a 30-day window.
    snap = (
        select(
            TsPageCache.computed_at.label("computed_at"),
            TsPageCache.payload[path].astext.label("value"),
            TsPageCache.payload["version"].astext.label("version"),
        )
        .where(
            TsPageCache.cache_key == cache_key,
            TsPageCache.computed_at >= window_start,
            TsPageCache.computed_at <= cutoff,
        )
        .cte("snap")
        .prefix_with("MATERIALIZED")
    )
    step = func.date_trunc(bucket, snap.c.computed_at)
    rows = (
        await session.execute(
            select(step.label("t"), snap.c.value, snap.c.version)
            .where(snap.c.value.isnot(None))
            .distinct(step)
            .order_by(step, snap.c.computed_at.desc())
        )
    ).all()
    return [
        (t.isoformat(), float(v), int(ver) if ver is not None else None)
        for t, v, ver in rows
    ]


_RECENT_TTL_SEC = 60.0
_RECENT_MAX_ENTRIES = 1024
_recent: dict[tuple, tuple[float, Points]] = {}
_recent_locks: dict[tuple, asyncio.Lock] = {}


def _clock() -> float:
    return time.monotonic()


async def _memoized(key: tuple, compute: Callable[[], Awaitable[Points]]) -> Points:
    """Per-process 60 s memo. Concurrent misses on one key share a single
    query (per-key lock) instead of each holding a pool connection for it."""
    hit = _recent.get(key)
    if hit is not None and _clock() - hit[0] < _RECENT_TTL_SEC:
        return hit[1]
    lock = _recent_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _recent.get(key)
        if hit is not None and _clock() - hit[0] < _RECENT_TTL_SEC:
            return hit[1]
        rows = await compute()
        if len(_recent) >= _RECENT_MAX_ENTRIES:
            _recent.clear()
            _recent_locks.clear()
        _recent[key] = (_clock(), rows)
        return rows


async def trend_series(
    session: AsyncSession,
    cache_key: str,
    path: tuple[str, ...],
    days: int,
    *,
    as_of: datetime | None = None,
    bucket: str = "hour",
) -> Points:
    """Hourly (date, value, version) trend for one JSONB ``path`` of a board
    cache key, read from its slim companion (``trend_store.slim_key``) instead of
    detoasting the full board blob once per snapshot in the window. The full key
    is read only for the part of the window the slim series has not reached yet
    (it accrues forward from deploy; ``board-trend-backfill`` seeds it).

    ``days`` is capped at ``trend_store.TREND_DAYS`` (90). The whole read (slim
    series, coverage probe, gap fill) runs under one ``PMI_API_HISTORY_TIMEOUT_MS``
    budget — a slow disk degrades to an empty series, not a 500. Ending-now reads
    are memoized for 60 s: snapshots land every ~10 min, so a minute-stale series
    is invisible on a chart and concurrent callers share one query.
    """
    if not trend_store.covers(cache_key, path):
        raise ValueError(f"{path!r} is not in the slim trend projection for {cache_key!r}")
    days = min(days, trend_store.TREND_DAYS)

    def compute(cutoff: datetime) -> Awaitable[Points]:
        # One budget for the whole read (slim series, coverage probe, gap).
        return bounded(
            session,
            api_settings.history_timeout_ms,
            lambda: _trend_points(session, cache_key, path, cutoff, days, bucket),
        )

    try:
        if as_of is not None:
            return await compute(as_of)
        # A timeout raises through the memo, so an empty series is never cached.
        return await _memoized(
            ("trend", cache_key, path, days, bucket), lambda: compute(datetime.now(UTC))
        )
    except QueryTimeout:
        log.warning("api.history_timeout", cache_key=cache_key, path=list(path), days=days)
        return []


async def board_history_points(
    session: AsyncSession,
    index_id: str,
    path: tuple[str, ...],
    days: int,
    *,
    as_of: datetime | None = None,
    bucket: str = "hour",
) -> Points:
    """Seat-board history for one JSONB path (P(control), or one race's P(R)) —
    a thin wrapper mapping ``index_id`` to its ``senate-board:*`` key."""
    return await trend_series(
        session, board_key(index_id), path, days, as_of=as_of, bucket=bucket
    )


async def _trend_points(
    session: AsyncSession,
    cache_key: str,
    path: tuple[str, ...],
    cutoff: datetime,
    days: int,
    bucket: str,
) -> Points:
    window_start = cutoff - timedelta(days=days)
    slim = trend_store.slim_key(cache_key)
    # Inner (un-bounded) reader: the caller (trend_series) wraps this whole
    # function in one `bounded` savepoint, so the slim read, the coverage probe
    # and the gap read all share that single time budget.
    rows = await _scalar_points_query(session, slim, path, cutoff, days, bucket=bucket)
    # Coverage comes from the slim key's own rows, not from `rows`: a path with no
    # values (or an unknown entity) must not trigger the full-history scan.
    slim_start = await session.scalar(
        select(func.min(TsPageCache.computed_at)).where(
            TsPageCache.cache_key == slim,
            TsPageCache.computed_at >= window_start,
            TsPageCache.computed_at <= cutoff,
        )
    )
    if slim_start is not None and slim_start <= window_start + timedelta(hours=1):
        return rows
    gap_end = slim_start if slim_start is not None else cutoff
    gap_days = max(1, math.ceil((gap_end - window_start) / timedelta(days=1)))
    older = await _scalar_points_query(session, cache_key, path, gap_end, gap_days, bucket=bucket)
    first_bucket = _bucket_start(window_start.astimezone(UTC), bucket).isoformat()
    seen = {t for t, _v, _ver in rows}
    merged = [r for r in older if r[0] not in seen and r[0] >= first_bucket] + rows
    merged.sort(key=lambda r: r[0])
    return merged


def _bucket_start(ts: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)
