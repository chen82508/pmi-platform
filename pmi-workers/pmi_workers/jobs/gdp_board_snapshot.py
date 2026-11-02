"""gdp-board-snapshot job — write the US GDP board + per-period detail to
ts_page_cache, off the request hot path.

Every /gdp page now serves from here (like every other vertical): the board
(`gdp-board:{id}`, whose history also feeds the sentiment trend via
`scalar_points`) and one blob per period (`gdp-period:{id}:{period}` = anchor +
contracts + hourly series). The period series is the expensive bit (re-scores
build_board per hour off ts_price_snapshots) — it ran per request (~6s) and now
runs once here. Compute lives in `pmi_core.engine.gdp_compute`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.engine.gdp_compute import GDP_INDEX_ID, compute_gdp
from pmi_core.market_priority import market_ids_in, register_board_watch
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.gdp_board_snapshot")


@register("gdp-board-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    async with session_scope() as session:
        board, periods = await compute_gdp(session)
        board_key = f"gdp-board:{GDP_INDEX_ID}"
        session.add(TsPageCache(cache_key=board_key, payload=board, computed_at=computed_at))
        # Slim trend companion (trend_store): the /gdp/board sentiment series reads
        # this instead of detoasting the full board blob per snapshot over 90 days.
        if (slim := trend_store.slim_values(board_key, board, computed_at)) is not None:
            session.add(TsPageCache(**slim))
        # Lift the in-use set from the ids the payload already carries (no score →
        # not in the cron's component lane; see register_board_watch).
        watch_ids = market_ids_in(board)
        for period, detail in periods.items():
            watch_ids |= market_ids_in(detail)
            session.add(
                TsPageCache(
                    cache_key=f"gdp-period:{GDP_INDEX_ID}:{period}",
                    payload=detail,
                    computed_at=computed_at,
                )
            )
            # Scalar-cache pipeline (calendar-gated) for the period 90D chip: the
            # 14d `series` above re-scores build_board off ts_price_snapshots (too
            # heavy at 90d for this cadence), so persist this period's CURRENT
            # reading (the series' last point) as one tiny row per run. Its own
            # ts_page_cache history IS the long trend, read cheaply via
            # scalar_points in /gdp/period. retention keeps gdp-period-anchor:*.
            series = detail.get("series") or []
            reading = series[-1]["value"] if series else None
            if reading is not None:
                session.add(
                    TsPageCache(
                        cache_key=f"gdp-period-anchor:{GDP_INDEX_ID}:{period}",
                        payload={"reading": reading},
                        computed_at=computed_at,
                    )
                )
        watched = await register_board_watch(session, "gdp", watch_ids, now=computed_at, log=log)
        await session.commit()

    log.info(
        "gdp_board_snapshot.done",
        score=board["score"],
        n_anchors=board["n_anchors"],
        n_periods=len(periods),
        watched=watched,
        computed_at=computed_at.isoformat(),
    )
    return {
        "score": board["score"],
        "n_anchors": board["n_anchors"],
        "n_periods": len(periods),
        "watched": watched,
    }
