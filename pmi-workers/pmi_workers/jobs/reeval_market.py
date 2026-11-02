"""`reeval-market` job — WS-triggered single-market re-evaluation (CORR-4.6).

Enqueued by the pmi-ingest WS consumer when a trade lands on a market that is
a component of some current index (see ``polymarket_ws._maybe_enqueue_reeval``).
Like Tier 3 drift (CORR-5.2), this re-runs the pipeline *tick* — the factor
cache is append-only and stays valid; what a trade staled is the price /
weights / collapse, which a tick refreshes.

Storm control happens at three layers, so a hot market costs one tick per
index per `PMI_WS_REEVAL_MIN_INTERVAL_SEC`, not one per trade:
1. the WS consumer debounces per market before enqueuing,
2. `dedupe_key` collapses pending `reeval-market` / `score` jobs,
3. this job skips indexes whose latest score is already fresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pmi_core import queue
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.models import AuditEvaluation, CoreIndexDefinition, TsIndexScore
from sqlalchemy import select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.reeval_market")


@register("reeval-market")
async def run(market_id: int) -> dict:
    """Fan a single market's trade activity out to per-index score jobs."""
    market_id = int(market_id)
    fresh_cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.ws_reeval_min_interval_sec
    )

    async with session_scope() as session:
        defs = (
            await session.execute(
                select(CoreIndexDefinition.id, CoreIndexDefinition.index_id).where(
                    CoreIndexDefinition.is_current.is_(True)
                )
            )
        ).all()

        affected: list[str] = []
        skipped_fresh: list[str] = []
        for def_id, slug in defs:
            score = (
                await session.execute(
                    select(TsIndexScore)
                    .where(TsIndexScore.index_definition_id == def_id)
                    .order_by(TsIndexScore.as_of.desc())
                    .limit(1)
                )
            ).scalars().first()
            if score is None or not score.component_evaluation_ids:
                continue
            is_component = (
                await session.execute(
                    select(AuditEvaluation.id)
                    .where(
                        AuditEvaluation.id.in_(score.component_evaluation_ids),
                        AuditEvaluation.market_id == market_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if is_component is None:
                continue
            if score.as_of > fresh_cutoff:
                skipped_fresh.append(slug)
                continue
            affected.append(slug)

        for slug in affected:
            await queue.enqueue(
                session,
                "score",
                {"index_id": slug},
                dedupe_key=f"score:{slug}",
                priority=queue.PRIORITY_INTERACTIVE,
            )
        if affected:
            await queue.notify(session)

    log.info(
        "reeval_market.done",
        market_id=market_id,
        rescored=affected,
        skipped_fresh=skipped_fresh,
    )
    return {"market_id": market_id, "enqueued": affected, "skipped_fresh": skipped_fresh}
