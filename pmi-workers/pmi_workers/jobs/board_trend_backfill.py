"""board-trend-backfill — one-time (re-runnable) seed/reconcile of every
vertical's slim trend history (``*-trend:*``, see
:mod:`pmi_core.engine.trend_store`) from the existing full board rows.

The snapshot jobs write the slim row going forward; without this the history
readers keep falling back to the full rows for the pre-deploy part of every
window, which is the slow read the slim series exists to remove. Covers all
trend families (war, maga, gdp-board, climate-*, esports-match, the seat boards).

For each full source key it REBUILDS the window's slim rows (delete + re-project)
rather than inserting only the missing ones. Rebuilding is what lets a widened
projection reach rows that already have a narrower slim row — e.g. maga-trend
predates the per-state fields, so insert-missing would leave those old rows
without ``states.*`` and the readers would see a silent gap. Idempotent: a second
run reproduces the identical slim rows. Projection runs in Python
(``trend_store.project``) because the war/maga specs keep nested per-entity
subtrees; the full rows are streamed server-side (``yield_per``) so peak memory is
one full payload, not the whole window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.models import TsPageCache
from sqlalchemy import delete, func, insert, select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.board_trend_backfill")


@register("board-trend-backfill", queue="batch")
async def run(days: int = trend_store.TREND_DAYS) -> dict:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rebuilt: dict[str, int] = {}
    key_family = func.split_part(TsPageCache.cache_key, ":", 1)
    async with session_scope() as session:
        # Every FULL source key in the retention window (family has a spec; slim
        # keys like `war-trend` are never spec families, so they are excluded).
        source_keys = (
            await session.execute(
                select(TsPageCache.cache_key)
                .where(
                    key_family.in_(trend_store.source_families()),
                    TsPageCache.computed_at >= cutoff,
                )
                .distinct()
            )
        ).scalars().all()

        for src in source_keys:
            slim = trend_store.slim_key(src)
            await session.execute(
                delete(TsPageCache).where(
                    TsPageCache.cache_key == slim, TsPageCache.computed_at >= cutoff
                )
            )
            # Stream the full rows one at a time (bounded memory) and accumulate
            # only the small projected rows; insert once, after the cursor closes
            # — never interleave writes with an open server-side cursor.
            result = await session.stream(
                select(TsPageCache.payload, TsPageCache.computed_at)
                .where(TsPageCache.cache_key == src, TsPageCache.computed_at >= cutoff)
                .execution_options(yield_per=500)
            )
            projected = [
                {
                    "cache_key": slim,
                    "payload": trend_store.project(src, payload),
                    "computed_at": computed_at,
                }
                async for payload, computed_at in result
            ]
            if projected:
                await session.execute(insert(TsPageCache), projected)
            await session.commit()  # bound the transaction; make progress durable per key
            rebuilt[src] = len(projected)

    log.info("board_trend_backfill.done", rebuilt=rebuilt, total=sum(rebuilt.values()))
    return {"rebuilt": rebuilt, "total": sum(rebuilt.values())}
