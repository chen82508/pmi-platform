"""senate-board-snapshot job — pre-compute senate board into ts_page_cache.

Uses MAGA method: reads P(R wins) per state directly from the MAGA cache
(all real-money venues via parse_state_race), adds Senate holdover geometry,
then runs Poisson-binomial — same approach as governor-board-snapshot.

Senate 2026 geometry (verified against Class I/II/III roster + OH/FL specials):
  Contested: 35 seats on the 2026 ballot (33 Class II + OH special + FL special)
  Holdover R: 31 (Class I 14 + Class III 19 - 2 specials on ballot)
  Holdover D: 34 (Class I 19 + Class III 15)
  Tie-break:  R (VP Republican through Jan 2029 → GOP controls at 50)
  Correlation: 0.5 (national-swing prior)
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.seat_distribution import band_counts, classify_band, compute_seat_distribution
from pmi_core.models import TsPageCache
from sqlalchemy import insert, select

from pmi_workers.jobs._board_series import (
    control_prob_series,
    current_version,
    write_board_trend,
)
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.senate_board_snapshot")

CYCLE_YEAR = 2026
CACHE_KEY = "senate-board:us-senate-2026-republican-seats"

_TOTAL_SEATS = 100
_MAJORITY_THRESHOLD = 51
_HOLDOVER_R = 31
_HOLDOVER_D = 34
_TIE_BREAK = "R"
_CORRELATION = 0.5

# The 35 seats actually on the 2026 ballot: 33 Class II regulars + the OH and FL
# specials. ONLY these are estimated (contested); the other 65 seats are fixed
# holdovers (_HOLDOVER_R/_HOLDOVER_D). Filtering to this roster stops the MAGA
# cache's non-2026 senate groups from being double-counted — the bug that
# inflated the field to 35+... = 115 seats and let P(R)+P(D) exceed 100%.
_CONTESTED_STATES_2026 = frozenset(
    {
        # 33 Class II (up 2026)
        "AL",
        "AK",
        "AR",
        "CO",
        "DE",
        "GA",
        "ID",
        "IL",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MA",
        "MI",
        "MN",
        "MS",
        "MT",
        "NE",
        "NH",
        "NJ",
        "NM",
        "NC",
        "OK",
        "OR",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "VA",
        "WV",
        "WY",
        # 2026 specials
        "OH",
        "FL",
    }
)


@register("senate-board-snapshot", queue="snapshot")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    """Read MAGA senate groups + holdovers → Poisson-binomial → ts_page_cache."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        maga_payload = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"maga:{cycle_year}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if not maga_payload:
            log.warning("senate_board_snapshot.no_maga_cache")
            return {"error": "no maga cache — run maga-snapshot first"}

        senate_groups = [g for g in maga_payload.get("groups", []) if g.get("office") == "senate"]
        if not senate_groups:
            log.warning("senate_board_snapshot.no_senate_groups")
            return {"error": "no senate groups in maga cache"}

        contested_probs: list[float] = []
        races: list[dict] = []
        prob_by_state: dict[str, float] = {}

        seen_states: set[str] = set()
        for i, g in enumerate(sorted(senate_groups, key=lambda x: x["state_code"])):
            code = g["state_code"]
            # Only estimate the 35 seats on the 2026 ballot; skip non-2026
            # groups (double-counted with the holdovers) and duplicate states.
            if code not in _CONTESTED_STATES_2026 or code in seen_states:
                continue
            seen_states.add(code)
            heat = float(g["heat"])
            prob_r = heat / 100.0

            contested_probs.append(prob_r)
            prob_by_state[code] = heat
            races.append(
                {
                    "market_id": -(i + 1),
                    "title": g["base_question"],
                    "prob_r": round(heat, 2),
                    "band": classify_band(prob_r),
                    "volume_24h": g.get("volume_24h"),
                    "state": code,
                    "matchup": None,
                    # Same quantity as prob_r (both are the maga group heat), so the
                    # group's 14d diff (D7) is this race's honest Δ14d.
                    "delta_14d": g.get("delta_14d"),
                    # §F(a): no real-money liquidity this tick (standing quotes), and
                    # whether the incumbent-hold prior replaced the thin read.
                    "untraded": bool(g.get("no_market", False)),
                    "prior_applied": bool(g.get("prior_applied", False)),
                    "contracts": g.get("n_markets", 0),
                    "exchanges": g.get(
                        "exchanges", []
                    ),  # venues backing this race (maga group carries them)
                }
            )

        dist = compute_seat_distribution(
            contested_probs,
            holdover_r=_HOLDOVER_R,
            holdover_d=_HOLDOVER_D,
            total_seats=_TOTAL_SEATS,
            majority_threshold=_MAJORITY_THRESHOLD,
            tie_break=_TIE_BREAK,
            correlation=_CORRELATION,
        )
        counts = band_counts(contested_probs, holdover_r=_HOLDOVER_R, holdover_d=_HOLDOVER_D)
        d_secured = counts["safe-d"] + counts["likely-d"] + counts["lean-d"]
        r_secured = counts["safe-r"] + counts["likely-r"] + counts["lean-r"]

        # Add holdover states to map (shown as fixed R/D, dimmed on map)
        # We don't know per-state holdover party from MAGA, so skip individual holdovers —
        # the overall holdover_r/d counts drive the distribution correctly.

        p_r_control = round(dist.p_r_control * 100.0, 2)
        series_14d = await control_prob_series(session, CACHE_KEY, computed_at, p_r_control)

        payload = {
            "index_id": "us-senate-2026-republican-seats",
            "version": await current_version(session, "us-senate-2026-republican-seats"),
            "as_of": computed_at.isoformat(),
            "p_r_majority": round(dist.p_r_majority * 100.0, 2),
            "p_d_majority": round(dist.p_d_majority * 100.0, 2),
            "p_r_control": p_r_control,
            "p_d_control": round(dist.p_d_control * 100.0, 2),
            "tie_break": _TIE_BREAK,
            "correlation": _CORRELATION,
            "expected_r_seats": round(dist.expected_r_seats, 2),
            "stdev_r_seats": round(dist.stdev_r_seats, 2),
            "total_seats": _TOTAL_SEATS,
            "majority_threshold": _MAJORITY_THRESHOLD,
            "holdover_r": _HOLDOVER_R,
            "holdover_d": _HOLDOVER_D,
            "counts": counts,
            "d_secured": d_secured,
            "r_secured": r_secured,
            "tossups": counts["tossup"],
            "n_contested": dist.n_contested,
            "races": races,
            "prob_by_state": prob_by_state,
            "series_14d": series_14d,
        }

        await session.execute(
            insert(TsPageCache).values(
                cache_key=CACHE_KEY,
                payload=payload,
                computed_at=computed_at,
            )
        )
        await write_board_trend(session, CACHE_KEY, payload, computed_at)
        await session.commit()

    log.info(
        "senate_board_snapshot.done",
        n_contested=dist.n_contested,
        computed_at=computed_at.isoformat(),
    )
    return {"n_contested": dist.n_contested}
