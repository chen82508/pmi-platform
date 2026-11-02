"""MAGA vertical composition — the top two levels, computed in the API layer.

The three MAGA chamber indexes (maga-senate / maga-governor / maga-house-2026)
each score independently via ``formula: entity_lean_mean`` and persist:
  * a national chamber lean      → ``ts_index_scores.score``
  * per-state / per-district heats → ``ts_index_scores.breakdown``

This module composes those persisted outputs into the two cross-chamber views
the website shows, WITHOUT re-running any scoring (Danny 2026-06-23: top MAGA is
an API-layer composition, not a fourth index):

  * **Top MAGA PMI (L4)** — equal-weight mean of the three chamber national
    leans. Every chamber counts once.
  * **State choropleth (L3 horizontal)** — per state, the equal-weight mean of
    that state's lean across the chambers that cover it.

Both are equal-weight, matching the cross-race rule the chamber indexes already
use internally. Pure-Python, no DB / network — the route hands in the persisted
scores + breakdowns.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def top_maga_pmi(chamber_scores: Iterable[float | None]) -> float | None:
    """Equal-weight mean of the present chamber national leans (L4).

    ``None`` chamber scores (an index that produced no score this tick) are
    skipped so a missing chamber never drags the top value to 0. Returns
    ``None`` only when every chamber is missing.
    """
    present = [s for s in chamber_scores if s is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def state_heats_from_breakdown(breakdown: dict | None) -> dict[str, float]:
    """``{state_name: heat}`` from an ``entity_lean_mean`` breakdown.

    Uses ``breakdown["groups"]`` when present (House: the state-level rollup of
    its districts) and falls back to ``breakdown["entities"]`` (Senate /
    Governor: the entity key IS the state name). Empty dict for a breakdown that
    carries neither (e.g. a below-min_components tick).
    """
    if not breakdown:
        return {}
    src = breakdown.get("groups") or breakdown.get("entities") or {}
    return {key: row["heat"] for key, row in src.items() if "heat" in row}


def compose_state_choropleth(
    chamber_breakdowns: Iterable[dict | None],
) -> dict[str, float]:
    """Per-state combined heat = equal-weight mean across covering chambers.

    A state colored by senate 70, governor 50, house 60 → 60.0. A state present
    in only one chamber takes that chamber's heat. Keys are canonical Title-Case
    state names, shared across all three chamber breakdowns.
    """
    by_state: dict[str, list[float]] = defaultdict(list)
    for breakdown in chamber_breakdowns:
        for state, heat in state_heats_from_breakdown(breakdown).items():
            by_state[state].append(heat)
    return {
        state: round(sum(heats) / len(heats), 2) for state, heats in by_state.items()
    }
