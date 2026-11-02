"""worlds-snapshot job — pre-compute the LoL Worlds tournament board + per-match
detail into ts_page_cache, off the request hot path.

Same pattern as every other vertical (esports/sports/war/…): run the two-layer
fold once every ~10 min and stash the result so the /worlds route serves each page
in a single latest-wins read, with no per-request compute.
  * `worlds-board:{tid}`  — the whole tournament (structure from YAML + live odds +
    resolved results), one cache row per tournament id.
  * `worlds-match:{slug}` — one match (match + holdings + win-% trend), feeding the
    match detail page.

Compute lives in `pmi_core.engine.worlds_compute`. The in-use set is lifted from
the market ids the payload already carries, so the priority cron keeps the live
Worlds contracts fresh (same as esports).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.worlds_compute import compute_worlds
from pmi_core.market_priority import market_ids_in, register_board_watch
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.worlds_snapshot")


_TOURNAMENTS = ["lol-worlds-2026"]


@register("worlds-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    out: dict[str, dict] = {}
    watch_ids: set[int] = set()
    async with session_scope() as session:
        for tid in _TOURNAMENTS:
            board, details = await compute_worlds(session, tid)
            watch_ids |= market_ids_in(board)
            session.add(
                TsPageCache(
                    cache_key=f"worlds-board:{tid}",
                    payload=board,
                    computed_at=computed_at,
                )
            )
            for slug, detail in details.items():
                watch_ids |= market_ids_in(detail)
                session.add(
                    TsPageCache(
                        cache_key=f"worlds-match:{slug}",
                        payload=detail,
                        computed_at=computed_at,
                    )
                )
            out[tid] = {
                "stages": len(board.get("stages", [])),
                "live": sum(len(s.get("live_matches", [])) for s in board.get("stages", [])),
                "results": sum(len(s.get("results", [])) for s in board.get("stages", [])),
                "details": len(details),
            }
        watched = await register_board_watch(session, "worlds", watch_ids, now=computed_at, log=log)
        await session.commit()

    log.info("worlds_snapshot.done", watched=watched, **{t: v for t, v in out.items()})
    return {**out, "watched": watched}
