"""governor-board-snapshot job — pre-compute governor board into ts_page_cache.

Reads P(R wins) per state from the latest MAGA cache (governor groups),
adds holdover governors (not up in 2026), then runs Poisson-binomial to
get projected seat distribution — identical concept to senate board.

Holdover governors (verified from 2023/2024 elections; 2025 NJ/VA estimated):
  R: LA, MS (2023) + IN, MO, MT, ND, UT, WV (2024) + VA (2025 est.) = 9
  D: KY (2023) + DE, NC, WA (2024) + NJ (2025 est.)                 = 5
  Total: 14 holdover + 36 contested (2026 calendar) = 50
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

log = structlog.get_logger("pmi_workers.jobs.governor_board_snapshot")

CYCLE_YEAR = 2026
# Same cache-key shape as senate/house boards (senate-board:<index_id>) so the
# generic /indexes/{id}/senate-board endpoint serves all three chambers uniformly.
CACHE_KEY = "senate-board:us-governor-2026-republican-seats"

# Current governors NOT up in 2026.
# Sources: 2023 winners (2027 cycle), 2024 winners (2028 cycle), 2025 winners (2029 cycle).
# 2025 NJ/VA are estimated (NJ→D lean, VA→R lean); update when official results confirmed.
_HOLDOVER_R = {"LA", "MS", "IN", "MO", "MT", "ND", "UT", "WV", "VA"}  # 9 states
_HOLDOVER_D = {"KY", "DE", "NC", "WA", "NJ"}  # 5 states

_TOTAL_SEATS = 50
_MAJORITY_THRESHOLD = 26


@register("governor-board-snapshot", queue="snapshot")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    """Read MAGA governor groups + holdovers → Poisson-binomial → ts_page_cache."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        # ── Read latest MAGA cache for governor groups ───────────────────────
        maga_payload = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"maga:{cycle_year}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if not maga_payload:
            log.warning("governor_board_snapshot.no_maga_cache")
            return {"error": "no maga cache — run maga-snapshot first"}

        # ── Extract governor groups ──────────────────────────────────────────
        gov_groups = [g for g in maga_payload.get("groups", []) if g.get("office") == "governor"]
        if not gov_groups:
            log.warning("governor_board_snapshot.no_governor_groups")
            return {"error": "no governor groups in maga cache"}

        # ── Build contested probabilities and per-race data ──────────────────
        contested_probs: list[float] = []
        races: list[dict] = []
        prob_by_state: dict[str, float] = {}

        holdover_states = _HOLDOVER_R | _HOLDOVER_D
        seen_states: set[str] = set()
        for i, g in enumerate(sorted(gov_groups, key=lambda x: x["state_code"])):
            code = g["state_code"]
            # Only estimate the 36 states electing a governor in 2026: skip the
            # 14 holdover states (their seats are fixed, counted separately) and
            # any duplicate group — else contested + holdover overshoots 50.
            if code in holdover_states or code in seen_states:
                continue
            seen_states.add(code)
            heat = float(g["heat"])  # 0–100, P(R wins) × 100
            prob_r = heat / 100.0

            contested_probs.append(prob_r)
            prob_by_state[code] = heat
            races.append(
                {
                    "market_id": -(i + 1),  # negative IDs: unique, won't collide with real markets
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

        holdover_r = len(_HOLDOVER_R)
        holdover_d = len(_HOLDOVER_D)

        dist = compute_seat_distribution(
            contested_probs,
            holdover_r=holdover_r,
            holdover_d=holdover_d,
            total_seats=_TOTAL_SEATS,
            majority_threshold=_MAJORITY_THRESHOLD,
            tie_break=None,
            correlation=0.3,  # lower correlation than senate (governors are more independent)
        )
        counts = band_counts(
            contested_probs,
            holdover_r=holdover_r,
            holdover_d=holdover_d,
        )

        d_secured = counts["safe-d"] + counts["likely-d"] + counts["lean-d"]
        r_secured = counts["safe-r"] + counts["likely-r"] + counts["lean-r"]

        # prob_by_state only contains contested states (2026 elections).
        # Holdover states are absent → dimmed on the map via dimMissing.

        p_r_control = round(dist.p_r_control * 100.0, 2)
        series_14d = await control_prob_series(session, CACHE_KEY, computed_at, p_r_control)

        payload = {
            "index_id": "us-governor-2026-republican-seats",
            "version": await current_version(session, "us-governor-2026-republican-seats"),
            "as_of": computed_at.isoformat(),
            "p_r_majority": round(dist.p_r_majority * 100.0, 2),
            "p_d_majority": round(dist.p_d_majority * 100.0, 2),
            "p_r_control": p_r_control,
            "p_d_control": round(dist.p_d_control * 100.0, 2),
            "tie_break": None,
            "correlation": 0.3,
            "expected_r_seats": round(dist.expected_r_seats, 2),
            "stdev_r_seats": round(dist.stdev_r_seats, 2),
            "total_seats": _TOTAL_SEATS,
            "majority_threshold": _MAJORITY_THRESHOLD,
            "holdover_r": holdover_r,
            "holdover_d": holdover_d,
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
        "governor_board_snapshot.done",
        n_contested=dist.n_contested,
        holdover_r=holdover_r,
        holdover_d=holdover_d,
        computed_at=computed_at.isoformat(),
    )
    return {"n_contested": dist.n_contested}
