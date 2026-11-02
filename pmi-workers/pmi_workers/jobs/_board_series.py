"""Shared helper: build a control-/majority-probability 14-day trend for a seat
board from its OWN snapshot history in ts_page_cache.

Each `*-board-snapshot` run appends one ts_page_cache row carrying that tick's
``p_r_control``. To draw a trailing-N-day trend we read those rows back,
downsample to one point per calendar day (latest tick per day wins), and append
the current tick as the final point. Grows from ~2 points to N as history
accumulates. Plotting p_r_control (not the stored E[seats] score) makes the
trend read the SAME quantity as the board's headline GOP-control tile.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pmi_core.engine.board_trend import slim_payload, trend_key_for_board_key
from pmi_core.models import CoreIndexDefinition, TsPageCache
from sqlalchemy import Float, insert, select
from sqlalchemy.ext.asyncio import AsyncSession


async def current_version(session: AsyncSession, index_id: str) -> int:
    """SCD2 version of ``index_id``'s live definition, or 1 if it has none.

    Board snapshots don't run the pipeline — they compute from the MAGA cache
    with geometry pinned in the job — but the payload still reports which
    definition version produced it, and consumers read that as lineage. The
    three seat boards used to hardcode 1 while the live defs had moved to v10
    and v4.
    """
    version = await session.scalar(
        select(CoreIndexDefinition.version).where(
            CoreIndexDefinition.index_id == index_id,
            CoreIndexDefinition.is_current.is_(True),
        )
    )
    return int(version) if version is not None else 1


async def write_board_trend(
    session: AsyncSession, cache_key: str, payload: dict, computed_at: datetime
) -> None:
    """Append the slim history row (see pmi_core.engine.board_trend) that goes
    with a full board snapshot. Same computed_at, so both series line up."""
    await session.execute(
        insert(TsPageCache).values(
            cache_key=trend_key_for_board_key(cache_key),
            payload=slim_payload(payload),
            computed_at=computed_at,
        )
    )


async def _daily_control(
    session: AsyncSession, key: str, start: datetime, end: datetime | None = None
) -> list[tuple[datetime, float | None]]:
    stmt = select(
        TsPageCache.computed_at,
        TsPageCache.payload["p_r_control"].astext.cast(Float),
    ).where(TsPageCache.cache_key == key, TsPageCache.computed_at >= start)
    if end is not None:
        stmt = stmt.where(TsPageCache.computed_at < end)
    return list((await session.execute(stmt.order_by(TsPageCache.computed_at))).all())


async def control_prob_series(
    session: AsyncSession,
    cache_key: str,
    computed_at: datetime,
    current_p_r_control: float,
    days: int = 14,
) -> list[float]:
    """Last ``days`` daily p_r_control values for ``cache_key`` (oldest→newest),
    with ``current_p_r_control`` as today's point.

    Reads the slim trend key; the full board key is read only for the part of
    the window older than the slim series' first row."""
    start = computed_at - timedelta(days=days)
    rows = await _daily_control(session, trend_key_for_board_key(cache_key), start)
    slim_start = rows[0][0] if rows else None
    if slim_start is None or slim_start > start + timedelta(days=1):
        rows = await _daily_control(session, cache_key, start, slim_start) + rows

    by_day: dict = {}
    for ts, p_r_control in rows:
        if p_r_control is not None:
            by_day[ts.date()] = float(p_r_control)
    by_day[computed_at.date()] = current_p_r_control

    return [by_day[d] for d in sorted(by_day)][-days:]
