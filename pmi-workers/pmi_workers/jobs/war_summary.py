"""war-summary job — the daily deterministic war note (D6).

Runs once a day (crontab 00:10 UTC) so the summary reads as a stable daily
editorial note, not a 10-minute ticker. Reads the latest enriched
``war:{cycle}`` payload and stashes:

  * ``war-summary:{cycle}``              — the world page's paragraphs + citations
  * ``war-region-summary:{rid}:{cycle}`` — one per region

Composition is pure template-from-numbers (`engine.war_summary`, the §F
deterministic-template decision): comparisons are computed in Python, facts
the payload can't support are omitted, and citations carry real
core_markets URLs already baked into the roster's top contracts. Latest-only
cache keys — no history reader, so default retention applies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.war_summary import compose_region_summary, compose_world_summary
from pmi_core.models import TsPageCache
from sqlalchemy import insert, select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.war_summary")

CYCLE_YEAR = 2026


@register("war-summary", queue="batch")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        payload = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"war:{cycle_year}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if not payload:
            log.warning("war_summary.no_war_cache")
            return {"error": "no war cache — run war-snapshot first"}

        world = compose_world_summary(payload)
        rows: list[dict] = []
        if world is not None:
            world["generated_at"] = computed_at.isoformat()
            rows.append(
                {
                    "cache_key": f"war-summary:{cycle_year}",
                    "payload": world,
                    "computed_at": computed_at,
                }
            )
        n_regions = 0
        for rid in payload.get("regions") or {}:
            summary = compose_region_summary(payload, rid)
            if summary is None:
                continue
            summary["generated_at"] = computed_at.isoformat()
            rows.append(
                {
                    "cache_key": f"war-region-summary:{rid}:{cycle_year}",
                    "payload": summary,
                    "computed_at": computed_at,
                }
            )
            n_regions += 1
        if rows:
            await session.execute(insert(TsPageCache), rows)
        await session.commit()

    log.info(
        "war_summary.done",
        world=world is not None,
        regions=n_regions,
        computed_at=computed_at.isoformat(),
    )
    return {"world": world is not None, "regions": n_regions}
