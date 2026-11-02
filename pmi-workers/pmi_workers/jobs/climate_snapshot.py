"""climate-snapshot job — pre-compute the Climate & Weather vertical into
ts_page_cache, off the request hot path (skill §9 golden rule).

Writes `climate-hub` (headline + a card per board) and `climate-board:{board}`
(one blob per sub-board = score + anchors + holdings). Compute lives in
`pmi_core.engine.climate_compute` so this worker and the pmi-api cold-cache
fallback share one implementation. Board coverage grows slice-by-slice
(Global Warming first).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.engine.climate_compute import compute_climate
from pmi_core.market_priority import market_ids_in, register_board_watch
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.climate_snapshot")


@register("climate-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    async with session_scope() as session:
        hub, boards = await compute_climate(session)
        session.add(TsPageCache(cache_key="climate-hub", payload=hub, computed_at=computed_at))
        # Slim trend companions (trend_store): the /climate readers plot score /
        # reading series off these, not the full board/question blobs.
        if (slim := trend_store.slim_values("climate-hub", hub, computed_at)) is not None:
            session.add(TsPageCache(**slim))
        # Lift the in-use set from the ids the payloads carry (no score → not in the
        # cron's component lane; see register_board_watch). questions are popped off
        # the board blob, so walk them too.
        watch_ids = market_ids_in(hub)
        n_q = 0
        for board, detail in boards.items():
            questions = detail.pop("questions", {})  # store per-question, keep board blob lean
            watch_ids |= market_ids_in(detail) | market_ids_in(questions)
            board_key = f"climate-board:{board}"
            session.add(TsPageCache(cache_key=board_key, payload=detail, computed_at=computed_at))
            if (slim := trend_store.slim_values(board_key, detail, computed_at)) is not None:
                session.add(TsPageCache(**slim))
            for anchor, q in questions.items():
                q_key = f"climate-question:{board}__{anchor}"
                q_payload = {**q, "board": board, "key": anchor}
                session.add(TsPageCache(cache_key=q_key, payload=q_payload, computed_at=computed_at))
                if (slim := trend_store.slim_values(q_key, q_payload, computed_at)) is not None:
                    session.add(TsPageCache(**slim))
                n_q += 1
        watched = await register_board_watch(session, "climate", watch_ids, now=computed_at, log=log)
        await session.commit()

    log.info(
        "climate_snapshot.done",
        score=hub["score"],
        n_boards=len(boards),
        n_questions=n_q,
        watched=watched,
        computed_at=computed_at.isoformat(),
    )
    return {"score": hub["score"], "n_boards": len(boards), "n_questions": n_q, "watched": watched}
