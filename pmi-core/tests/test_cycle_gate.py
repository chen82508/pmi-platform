"""entity_lean_mean cycle gates (stage B): per-market election_cycle + race_calendar.

Verifies the two gates wired into ``_entity_lean_aggregate`` when a def declares
``extraction.cycle``:
  * gate 1 — a market whose recovered cycle is a KNOWN different year is dropped
    (kalshi's yearless title + -28 ticker no longer lands in the 2026 index);
  * gate 2 — us_state / us_governor keep only race_calendar-contested states, so
    a fail-open (undetermined-cycle) market can't coin a phantom race.
Cycle-agnostic defs (cycle=None) are unchanged.

Senate rows use kalshi's real yearless party-aggregate form ("... Senate race in
<state>? - Republican party") so the cycle can only come from the ticker /
closes_at, which is the whole point.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    ExtractionSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
    LiquidityWeighting,
    WeightingSpec,
)
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir(group_by: str = "us_state", cycle: int | None = 2026) -> IndexDef:
    return IndexDef(
        id="cycle-gate-test",
        version=1,
        title="Cycle Gate",
        selectors=[KeywordSelector(type="keyword", terms=["senate"])],
        factors=[
            FactorSpec(id="gate", type="binary", prompt_ref="prompts/factors/x-v1", weight=100)
        ],
        extraction=ExtractionSpec(group_by=group_by, methods=["patterns"], cycle=cycle),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=True),
            min_components=1,
            formula="entity_lean_mean",
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method="quantile")),
    )


def _row(
    mid: int,
    title: str,
    price: float,
    *,
    venue: str = "kalshi",
    external_id: str | None = None,
    closes_at: datetime | None = None,
) -> MarketEvaluations:
    return MarketEvaluations(
        market=CoreMarket(
            id=mid,
            venue=venue,
            external_id=external_id or f"e{mid}",
            slug=None,
            title=title,
            raw=None,
            closes_at=closes_at,
        ),
        by_factor={
            "gate": AuditEvaluation(
                id=mid * 10,
                market_id=mid,
                index_definition_id=1,
                factor_id="gate",
                prompt_id=1,
                prompt_sha256="x" * 64,
                model_id="stub",
                value_numeric=1.0,
            )
        },
        last_price=price,
        liquidity=100.0,
    )


def _kalshi_senate(state: str) -> str:
    # Real kalshi yearless party-aggregate leg — cycle must come from the ticker.
    return f"Will Republicans win the Senate race in {state}? - Republican party"


# --------------------------------------------------------------------------
# gate 1 — per-market cycle drop
# --------------------------------------------------------------------------

def test_kalshi_2028_leg_dropped_from_2026_index() -> None:
    rows = [
        _row(1, _kalshi_senate("Texas"), 0.60, external_id="SENATETX-26-R"),       # 2026: kept
        _row(2, _kalshi_senate("California"), 0.05, external_id="SENATECA-28-R"),  # 2028: dropped
    ]
    ents = aggregate(rows, _ir()).breakdown["entities"]
    assert set(ents) == {"Texas"}


def test_cycle_agnostic_keeps_both() -> None:
    # cycle=None → old behaviour: California's 2028 leg is a member (and a phantom).
    rows = [
        _row(1, _kalshi_senate("Texas"), 0.60, external_id="SENATETX-26-R"),
        _row(2, _kalshi_senate("California"), 0.05, external_id="SENATECA-28-R"),
    ]
    ents = aggregate(rows, _ir(cycle=None)).breakdown["entities"]
    assert set(ents) == {"Texas", "California"}


# --------------------------------------------------------------------------
# gate 2 — race_calendar entity gate (fail-open market still filtered)
# --------------------------------------------------------------------------

def test_undetermined_cycle_phantom_state_dropped_by_calendar() -> None:
    # No ticker year, no title year, no closes_at → cycle None → kept by gate 1;
    # New York has no 2026 Senate race, so gate 2 (race_calendar) drops it.
    rows = [
        _row(1, _kalshi_senate("Texas"), 0.60, external_id="noyear1"),
        _row(2, _kalshi_senate("New York"), 0.05, external_id="noyear2"),
    ]
    ents = aggregate(rows, _ir()).breakdown["entities"]
    assert set(ents) == {"Texas"}


def test_governor_calendar_gate() -> None:
    # us_governor uses the governor calendar. Texas has a 2026 governor race,
    # Kentucky is an odd-year gubernatorial state (no 2026 race).
    rows = [
        _row(1, "Will the Republicans win the Texas gubernatorial race in 2026?", 0.7, venue="polymarket"),
        _row(2, "Will the Republicans win the Kentucky gubernatorial race in 2026?", 0.7, venue="polymarket"),
    ]
    ents = aggregate(rows, _ir(group_by="us_governor")).breakdown["entities"]
    assert "Texas" in ents
    assert "Kentucky" not in ents


# --------------------------------------------------------------------------
# closes_at fallback drives the gate when title/ticker are yearless
# --------------------------------------------------------------------------

def test_closes_at_cycle_fold() -> None:
    rows = [
        _row(1, _kalshi_senate("Texas"), 0.6, external_id="noyear",
             closes_at=datetime(2027, 11, 3, tzinfo=UTC)),   # → 2026, kept
        _row(2, _kalshi_senate("North Carolina"), 0.6, external_id="noyear2",
             closes_at=datetime(2029, 11, 7, tzinfo=UTC)),   # → 2028, dropped
    ]
    ents = aggregate(rows, _ir()).breakdown["entities"]
    assert set(ents) == {"Texas"}
