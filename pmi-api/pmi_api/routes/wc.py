"""World Cup 2026 matchup odds — GET /wc/matchups (retired).

The tournament ended on 2026-07-19 and every advance market has settled, so
the live board is gone: this route returns a fixed empty envelope and never
touches the DB. The old live query (three OR'd title regexes over core_markets)
took 15-40 s on a cold cache and, after the final, only matched other cups'
"Team to Advance" books that `build_board` then discarded. No snapshot of the
final board exists in ts_page_cache, so there is nothing to serve instead.

The route stays (rather than 404) so old clients keep getting a valid payload.
`pmi_api.wc_matchups` keeps the pure board logic and its unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query

from pmi_api.schemas import WcMatchupsEnvelope, WcMatchupsPayload, WcParams

router = APIRouter(prefix="/wc", tags=["wc"])

TOURNAMENT_ENDED = "2026-07-19"


@router.get("/matchups", response_model=WcMatchupsEnvelope)
async def wc_matchups(
    sweet_odds: float = Query(default=3.0, ge=1.01, le=1000.0),
    min_edge: float = Query(default=0.03, ge=0.0, le=1.0),
    max_age_h: int = Query(default=48, ge=1, le=168),
) -> WcMatchupsEnvelope:
    """Always empty: the tournament is over. Query params are accepted and
    echoed for backward compatibility; ``max_age_h`` no longer has an effect."""
    payload = WcMatchupsPayload(
        generated_at=datetime.now(UTC),
        matchups=[],
        teams={},
        params=WcParams(sweet_odds=sweet_odds, min_edge=min_edge),
    )
    return WcMatchupsEnvelope(
        summary=(
            f"WC-2026 concluded on {TOURNAMENT_ENDED}: all advance markets have "
            "settled, so there are no fixtures to price."
        ),
        data=payload,
    )
