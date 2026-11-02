"""insights-snapshot job — compute the weekly Insights issue and freeze it.

Runs daily in the batch lane (the gap-pairs self-join is the venue-spread cost,
~tens of seconds — too heavy for the hot path or a 10-min beat). Writes two keys:

* ``insight:latest`` — the current issue, what `/micah/insights` renders now.
* ``insight:issue:{iso_week}`` — the FROZEN archive. One key per ISO week: the
  current week's key is rewritten by each daily run until the week rolls over,
  then never changes again. Each week being its OWN cache_key is what makes the
  archive permanent — `page-cache-retention`'s latest-only rule keeps the newest
  rows PER key, so every past week's frozen issue survives (a week has ≤7 daily
  runs, well under keep_latest). This is what makes "Past issues" a real archive
  and lets the emailed weekly report render the identical frozen snapshot later.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.insights_compute import compute_insights
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.insights_snapshot")


@register("insights-snapshot", queue="batch")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    async with session_scope() as session:
        issue = await compute_insights(session)
        session.add_all(
            [
                TsPageCache(cache_key="insight:latest", payload=issue, computed_at=computed_at),
                TsPageCache(
                    cache_key=f"insight:issue:{issue['iso_week']}",
                    payload=issue,
                    computed_at=computed_at,
                ),
            ]
        )
        await session.commit()

    lead = issue.get("lead") or {}
    log.info(
        "insights_snapshot.done",
        iso_week=issue["iso_week"],
        chamber_spread=lead.get("spread_now"),
        gap_pairs=len((issue.get("gap_pairs") or {}).get("pairs", [])),
        settling=len((issue.get("settling") or {}).get("items", [])),
        readings=len(issue.get("readings") or []),
        computed_at=computed_at.isoformat(),
    )
    return {
        "iso_week": issue["iso_week"],
        "gap_pairs": len((issue.get("gap_pairs") or {}).get("pairs", [])),
        "readings": len(issue.get("readings") or []),
    }
