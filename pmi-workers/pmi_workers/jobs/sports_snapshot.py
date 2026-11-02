"""sports-snapshot job — pre-compute the Sports vertical boards into ts_page_cache.

Same pattern as every other vertical (esports/war/markets/…): run the fold once
every ~10 min and stash the result so the /sports/* route serves each page in a
single latest-wins read, with no per-request compute.
  * `sports-board:{sport}` — one sport's whole vertical, one cache row per sport
    (MLB — World Series board + playoff bracket; NFL — this week's games with
    moneyline/spread/total + Super Bowl futures). Keyed per sport so /sports/{sport}
    stays its own page and more sports slot in beside it.

Compute lives in `pmi_core.engine.sports_compute`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.sports_compute import compute_sports
from pmi_core.market_priority import register_board_watch
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.sports_snapshot")


_SPORTS = ["mlb", "nfl"]


@register("sports-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    out: dict[str, dict] = {}
    watch_ids: set[int] = set()
    async with session_scope() as session:
        for sport in _SPORTS:
            board, ids = await compute_sports(session, sport)
            watch_ids |= ids
            session.add(
                TsPageCache(
                    cache_key=f"sports-board:{sport}",
                    payload=board,
                    computed_at=computed_at,
                )
            )
            out[sport] = {
                "teams": sum(len(o.get("teams", [])) for o in board.get("outrights", [])),
                "games": len(board.get("games", [])),
            }
        # Stamp what we rendered for the priority cron (see register_board_watch).
        watched = await register_board_watch(session, "sports", watch_ids, now=computed_at, log=log)
        await session.commit()

    log.info("sports_snapshot.done", watched=watched, **{s: v for s, v in out.items()})
    return {**out, "watched": watched}
