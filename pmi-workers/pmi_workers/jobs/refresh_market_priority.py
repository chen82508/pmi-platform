"""refresh-market-priority — rebuild core_market_priority every 5 minutes.

Collects the in-use signals (current-index components, board/activity watches,
recent listings) into the small table the ingest pollers read their P0/P1 id
lists from, so no poller has to filter core_markets per cycle. Default lane: a
few PK-lookup statements, seconds not minutes. Logic lives in
pmi_core.market_priority; see docs/reports/priority_ingestion_blueprint.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pmi_core.db import session_scope
from pmi_core.market_priority import refresh_market_priority

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.refresh_market_priority")


@register("refresh-market-priority")
async def run(new_listing_hours: float = 48.0) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with session_scope() as session:
        result = await refresh_market_priority(
            session, now=now, new_listing_hours=new_listing_hours
        )
    summary = {
        "total": result.total,
        "by_tier": {str(k): v for k, v in sorted(result.by_tier.items())},
        "removed": result.removed,
        "watches_expired": result.watches_expired,
        "watermark_used": result.watermark_used,
        "watermark_taken": result.watermark_taken,
    }
    log.info("refresh_market_priority.done", **summary)
    return summary
