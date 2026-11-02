"""esports-snapshot job — pre-compute the esports match board + per-match detail
into ts_page_cache, off the request hot path.

Same pattern as every other vertical (war/markets/seats/gdp): the /esports/*
endpoints used to live-compute per request (re-fold ~hundreds of markets + scan
ts_price_snapshots for each trend, ~2.5s), the ONE vertical that never got a
snapshot. This runs the fold once every ~10 min and stashes:
  * `esports-board`        — the match list (feeds /esports/matches)
  * `esports-match:{slug}` — one match + holdings + trend (feeds the detail page)
so the route serves each page in a single latest-wins read.

Compute lives in `pmi_core.engine.esports_compute` so this worker and the pmi-api
cache-miss fallback share one implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.engine.esports_compute import compute_esports
from pmi_core.market_priority import market_ids_in, register_board_watch
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.esports_snapshot")


@register("esports-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    async with session_scope() as session:
        board, details = await compute_esports(session)
        session.add(
            TsPageCache(cache_key="esports-board", payload=board, computed_at=computed_at)
        )
        # Lift the in-use set from the ids the payload already carries (no score →
        # not in the cron's component lane; see register_board_watch).
        watch_ids = market_ids_in(board)
        for slug, detail in details.items():
            watch_ids |= market_ids_in(detail)
            match_key = f"esports-match:{slug}"
            session.add(TsPageCache(cache_key=match_key, payload=detail, computed_at=computed_at))
            # Slim trend companion (trend_store): the match volume sparkbar reads
            # this instead of detoasting the full match blob per snapshot.
            if (slim := trend_store.slim_values(match_key, detail, computed_at)) is not None:
                session.add(TsPageCache(**slim))
        watched = await register_board_watch(session, "esports", watch_ids, now=computed_at, log=log)
        await session.commit()

    n = len(board.get("matches", []))
    log.info("esports_snapshot.done", matches=n, details=len(details), watched=watched)
    return {"matches": n, "details": len(details), "watched": watched}
