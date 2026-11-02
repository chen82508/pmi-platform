"""house-board-snapshot job — pre-compute house board into ts_page_cache.

Uses MAGA method: reads P(R wins) per district directly from the MAGA cache
(all real-money venues via parse_state_race), then runs Poisson-binomial.
No holdovers — all 435 seats are contested every cycle.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.seat_distribution import band_counts, classify_band, compute_seat_distribution
from pmi_core.engine.seat_mapping import HOUSE_SEATS
from pmi_core.models import TsPageCache
from sqlalchemy import insert, select

from pmi_workers.jobs._board_series import (
    control_prob_series,
    current_version,
    write_board_trend,
)
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.house_board_snapshot")

CYCLE_YEAR = 2026
CACHE_KEY = "senate-board:us-house-2026-republican-seats"

_TOTAL_SEATS = 435
_MAJORITY_THRESHOLD = 218

# Non-voting House delegations — a delegate, not one of the 435 voting seats.
# MAGA carries a DC "district"; counting it overshoots 435 and tips P(R)+P(D)
# past 100%.
_NON_VOTING = frozenset({"DC", "PR", "GU", "VI", "AS", "MP"})


@register("house-board-snapshot", queue="snapshot")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    """Read MAGA house groups → Poisson-binomial → ts_page_cache."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        maga_payload = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"maga:{cycle_year}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if not maga_payload:
            log.warning("house_board_snapshot.no_maga_cache")
            return {"error": "no maga cache — run maga-snapshot first"}

        house_groups = [g for g in maga_payload.get("groups", []) if g.get("office") == "house"]
        if not house_groups:
            log.warning("house_board_snapshot.no_house_groups")
            return {"error": "no house groups in maga cache"}

        contested_probs: list[float] = []
        races: list[dict] = []
        prob_by_district: dict[str, float] = {}
        state_heats: dict[str, list[float]] = defaultdict(list)

        seen_districts: set[str] = set()
        for i, g in enumerate(
            sorted(house_groups, key=lambda x: (x["state_code"], x.get("district") or 0))
        ):
            code = g["state_code"]
            district = g.get("district") or 1
            heat = float(g["heat"])
            prob_r = heat / 100.0
            district_key = f"{code}-{district:02d}"
            # Skip non-voting delegations (DC etc.), dedup, and reject phantom
            # districts beyond a state's real seat count (e.g. a WA-34 parsed
            # out of a "vote count above 340K" market — WA has only 10). Any of
            # these pushes the field past 435 and P(R)+P(D) past 100%. Defensive:
            # extraction._district_key clamps per-state upstream too.
            seats = HOUSE_SEATS.get(code)
            if (
                code in _NON_VOTING
                or district_key in seen_districts
                or seats is None
                or not 1 <= district <= seats
            ):
                continue
            seen_districts.add(district_key)

            contested_probs.append(prob_r)
            prob_by_district[district_key] = heat
            state_heats[code].append(heat)
            races.append(
                {
                    "market_id": -(i + 1),
                    "title": g["base_question"],
                    "prob_r": round(heat, 2),
                    "band": classify_band(prob_r),
                    "volume_24h": g.get("volume_24h"),
                    "state": district_key,
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

        # State-level aggregation for map
        prob_by_state = {
            code: round(sum(heats) / len(heats), 2) for code, heats in state_heats.items()
        }

        dist = compute_seat_distribution(
            contested_probs,
            holdover_r=0,
            holdover_d=0,
            total_seats=_TOTAL_SEATS,
            majority_threshold=_MAJORITY_THRESHOLD,
            tie_break=None,
            correlation=0.5,  # national-swing prior, same as senate
        )
        counts = band_counts(contested_probs, holdover_r=0, holdover_d=0)
        d_secured = counts["safe-d"] + counts["likely-d"] + counts["lean-d"]
        r_secured = counts["safe-r"] + counts["likely-r"] + counts["lean-r"]

        p_r_control = round(dist.p_r_control * 100.0, 2)
        series_14d = await control_prob_series(session, CACHE_KEY, computed_at, p_r_control)

        payload = {
            "index_id": "us-house-2026-republican-seats",
            "version": await current_version(session, "us-house-2026-republican-seats"),
            "as_of": computed_at.isoformat(),
            "p_r_majority": round(dist.p_r_majority * 100.0, 2),
            "p_d_majority": round(dist.p_d_majority * 100.0, 2),
            "p_r_control": p_r_control,
            "p_d_control": round(dist.p_d_control * 100.0, 2),
            "tie_break": None,
            "correlation": None,
            "expected_r_seats": round(dist.expected_r_seats, 2),
            "stdev_r_seats": round(dist.stdev_r_seats, 2),
            "total_seats": _TOTAL_SEATS,
            "majority_threshold": _MAJORITY_THRESHOLD,
            "holdover_r": 0,
            "holdover_d": 0,
            "counts": counts,
            "d_secured": d_secured,
            "r_secured": r_secured,
            "tossups": counts["tossup"],
            "n_contested": dist.n_contested,
            "races": races,
            "prob_by_state": prob_by_state,
            "prob_by_district": prob_by_district,
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
        "house_board_snapshot.done",
        n_contested=dist.n_contested,
        computed_at=computed_at.isoformat(),
    )
    return {"n_contested": dist.n_contested}
