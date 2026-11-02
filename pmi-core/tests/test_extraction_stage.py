"""Unit tests for the CORR-1.8 declarative extraction stage.

Covers three layers, all pure-Python (no DB / LLM / network):

1. **IR validation** — the ``extraction`` block wiring rules in dsl/ir.py:
   which formulas accept/require it, and the llm_fallback ↔ extraction-factor
   coupling.
2. **us_house_district grammar** — engine/extraction.py district parsing
   (party-direct, party-proxy, candidate, at-large), label validation, and
   the precedence/merge behaviour shared with the senate grammar.
3. **entity_lean_mean aggregator** — per-entity liquidity-weighted heat and
   the top-line collapse.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

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
from pmi_core.engine.extraction import (
    extract_entities,
    parse_district_candidate_race,
    parse_district_extraction_label,
    parse_district_race,
    parse_governor_race,
)
from pmi_core.models import AuditEvaluation, CoreMarket

# ---------------------------------------------------------------------------
# IR validation
# ---------------------------------------------------------------------------

_GATE = FactorSpec(
    id="gate", type="binary", prompt_ref="prompts/factors/x-v1", weight=100
)
_EXTRACT_FACTOR = FactorSpec(
    id="race_extraction",
    type="extraction",
    prompt_ref="prompts/factors/x-extraction-v1",
    weight=None,
)


def _ir(**kw):
    base = dict(
        id="t",
        version=1,
        title="t",
        selectors=[KeywordSelector(type="keyword", terms=["x"])],
        factors=[_GATE],
    )
    base.update(kw)
    return IndexDef(**base)


def test_weighted_average_rejects_extraction_block() -> None:
    with pytest.raises(ValidationError, match="never consumes"):
        _ir(
            extraction=ExtractionSpec(group_by="us_state"),
            aggregation=AggregationSpec(formula="weighted_average_x_100"),
        )


def test_entity_lean_mean_requires_extraction_block() -> None:
    with pytest.raises(ValidationError, match="requires an extraction block"):
        _ir(aggregation=AggregationSpec(formula="entity_lean_mean"))


def test_llm_fallback_requires_extraction_factor() -> None:
    with pytest.raises(ValidationError, match="no factor has\n.*type 'extraction'|type 'extraction'"):
        _ir(
            extraction=ExtractionSpec(
                group_by="us_state", methods=["patterns", "llm_fallback"]
            ),
            aggregation=AggregationSpec(formula="seat_projection_sum"),
        )


def test_llm_fallback_ok_with_extraction_factor() -> None:
    ir = _ir(
        factors=[_GATE, _EXTRACT_FACTOR],
        extraction=ExtractionSpec(group_by="us_state", methods=["patterns", "llm_fallback"]),
        aggregation=AggregationSpec(formula="seat_projection_sum"),
    )
    assert ir.extraction is not None


def test_patterns_only_needs_no_extraction_factor() -> None:
    ir = _ir(
        extraction=ExtractionSpec(group_by="us_house_district", methods=["patterns"]),
        aggregation=AggregationSpec(formula="seat_projection_sum"),
    )
    assert ir.extraction.methods == ["patterns"]


def test_seat_projection_grandfathers_missing_block() -> None:
    # Pre-CORR-1.8 defs declared seat_projection_sum with no extraction block.
    ir = _ir(aggregation=AggregationSpec(formula="seat_projection_sum"))
    assert ir.extraction is None


# ---------------------------------------------------------------------------
# us_house_district grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        (
            "Will the Republican party win the California's 20th District general "
            "election for the US House of Representatives in 2026?",
            ("R", "California-20"),
        ),
        (
            "Will Democratic Primary Winner win the Alabama 5th District general "
            "election for the US House of Representatives in 2026?",
            ("D", "Alabama-5"),
        ),
        # primary (not general) — rejected by the tail
        (
            "Will the Republican party win the Texas 9th District Republican primary "
            "election for the US House of Representatives in 2026?",
            None,
        ),
        # margin bracket — rejected
        (
            "Will the margin of victory for Democrats in the Ohio's 3rd District "
            "House election be at least 49 percentage points?",
            None,
        ),
        # senate race — wrong office tail
        ("Will the Republicans win the Ohio Senate race in 2026?", None),
        # kalshi code form with party-aggregate leg (at-large → district 1)
        (
            "Will Democratic win the House race for AK-AL? - Democratic party",
            ("D", "Alaska-1"),
        ),
        (
            "Will Republican win the House race for AL-01? - Republican party",
            ("R", "Alabama-1"),
        ),
        # kalshi party-aggregate leg annotated with the nominee (not "- party"):
        # party read from the SUBJECT ("Will Democratic win …"), so it's kept.
        ("Will Democratic win the House race for AL-03? - Lee McInnis", ("D", "Alabama-3")),
        # a genuine candidate-SUBJECT leg still has no party-direct reading.
        ("Will Bill Hill win the House race for AK-AL?", None),
    ],
)
def test_parse_district_race(title, expected) -> None:
    assert parse_district_race(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        # polymarket / forecastex / predictit forms still parse
        ("Will the Republicans win the Texas gubernatorial race in 2026?", ("R", "Texas")),
        (
            "Will the Republican party win the Texas general election for governor in 2026?",
            ("R", "Texas"),
        ),
        (
            "Which party will win the 2026 election for governor of Ohio? - Democratic",
            ("D", "Ohio"),
        ),
        # kalshi: party in the question, price IS P(party wins) for the state
        ("Will the Democratic party win the governorship in Arizona", ("D", "Arizona")),
        ("Will the Republican party win the governorship in Vermont", ("R", "Vermont")),
        # kalshi with nominee annotation appended — still the party probability
        (
            "Will the Democratic party win the governorship in Alabama - Doug Jones",
            ("D", "Alabama"),
        ),
        ("Will the Democratic party win the governorship in New Hampshire", ("D", "New Hampshire")),
        # multi-state combo ("governorships of …") — not a single seat, skipped
        (
            "Will Democrats win the governorships of Pennsylvania, Michigan, Wisconsin, "
            "Georgia, Arizona, AND Nevada? - Yes",
            None,
        ),
    ],
)
def test_parse_governor_race(title, expected) -> None:
    assert parse_governor_race(title) == expected


def test_parse_district_candidate() -> None:
    assert parse_district_candidate_race(
        "Will Adam Gray win the California 13th District general election for "
        "the US House of Representatives in 2026?"
    ) == ("Adam Gray", "California-13")
    # party-direct subject is NOT a candidate
    assert (
        parse_district_candidate_race(
            "Will the Republican party win the Ohio 3rd District general election "
            "for the US House of Representatives in 2026?"
        )
        is None
    )


def test_parse_district_extraction_label_validates() -> None:
    ok = json.dumps({"party": "R", "state": "California", "district": 20})
    assert parse_district_extraction_label(ok) == ("R", "California-20")
    # at-large canonicalises to district "1" (so a "...1st District..." market and
    # an "...XX-AL..." market merge into one entity) — see _district_key.
    at_large = json.dumps({"party": "D", "state": "Wyoming", "district": "at-large"})
    assert parse_district_extraction_label(at_large) == ("D", "Wyoming-1")
    # implausible district / bad party / foreign state → dropped, never guessed
    assert parse_district_extraction_label(json.dumps({"party": "R", "state": "California", "district": 99})) is None
    assert parse_district_extraction_label(json.dumps({"party": "I", "state": "Ohio", "district": 1})) is None
    assert parse_district_extraction_label(json.dumps({"party": "R", "state": "Ontario", "district": 1})) is None
    assert parse_district_extraction_label("not json") is None


def test_district_merge_collapses_rd_sides_and_extends_via_extraction() -> None:
    spec = ExtractionSpec(group_by="us_house_district", methods=["patterns", "llm_fallback"])
    markets = [
        (1, "Will the Republican party win the California's 20th District general "
            "election for the US House of Representatives in 2026?", 0.70),
        (2, "Will the Democratic party win the California's 20th District general "
            "election for the US House of Representatives in 2026?", 0.30),
        # a phrasing no pattern understands, but the LLM extraction maps it
        (3, "GOP takes WV-02 in the midterms?", 0.65),
    ]
    extraction_hints = {3: ("R", "West Virginia-2")}
    seats = extract_entities(spec, markets, extraction_by_market=extraction_hints)
    by_key = {s.state: s for s in seats}
    # CA-20: R and D markets of the same district collapse to ONE seat, R price wins
    assert "California-20" in by_key
    assert by_key["California-20"].prob_r == pytest.approx(0.70)
    assert by_key["California-20"].source_party == "R"
    # extraction tier extends coverage to a district patterns missed
    assert "West Virginia-2" in by_key
    assert by_key["West Virginia-2"].prob_r == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# entity_lean_mean aggregator
# ---------------------------------------------------------------------------


def _lean_ir(
    method: str = "quantile", min_components: int = 1, index_id: str = "state-lean-test"
) -> IndexDef:
    return IndexDef(
        id=index_id,
        version=1,
        title="State Lean",
        selectors=[KeywordSelector(type="keyword", terms=["senate"])],
        factors=[
            FactorSpec(id="gate", type="binary", prompt_ref="prompts/factors/x-v1", weight=100)
        ],
        extraction=ExtractionSpec(group_by="us_state", methods=["patterns"]),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=True),
            min_components=min_components,
            formula="entity_lean_mean",
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method=method)),
    )


def _lean_row(mid: int, title: str, price: float, liquidity: float | None) -> MarketEvaluations:
    return MarketEvaluations(
        market=CoreMarket(
            id=mid, venue="polymarket", external_id=f"e{mid}", slug=None, title=title, raw=None
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
        liquidity=liquidity,
    )


def test_entity_lean_mean_per_entity_heat_and_breakdown() -> None:
    rows = [
        _lean_row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.80, 100.0),
        # same state, D side at price 0.30 → directed P(R)=0.70; liquidity 300
        _lean_row(2, "Will the Democrats win the Ohio Senate race in 2026?", 0.30, 300.0),
        _lean_row(3, "Will the Republicans win the Texas Senate race in 2026?", 0.60, 100.0),
    ]
    result = aggregate(rows, _lean_ir())
    assert result.breakdown["formula"] == "entity_lean_mean"
    ents = result.breakdown["entities"]
    # Ohio: both legs are party-direct on ONE venue (polymarket), so reduce_races
    # takes the simple mean of their directed prob_r — avg(0.80, 1-0.30) = 0.75 →
    # 75.0 (liquidity now weights ACROSS venues, not between a race's two legs).
    assert ents["Ohio"]["heat"] == pytest.approx(75.0)
    assert ents["Ohio"]["n_markets"] == 2
    assert ents["Ohio"]["weight"] == pytest.approx(400.0)  # 100 + 300 book depth
    assert ents["Texas"]["heat"] == pytest.approx(60.0)
    # score = liquidity-weighted mean of entity heats (no rollup declared):
    # Ohio 75.0 @w400, Texas 60.0 @w100 → (75*400 + 60*100)/500 = 72.0
    assert result.score == pytest.approx(72.0)
    assert result.component_count == 2  # two entities


def test_entity_lean_mean_excludes_zero_liquidity_race() -> None:
    # §F(a): a zero-liquidity race is flagged no_market and excluded from the
    # top-line lean. Displayed at its thin read, except a GROSS misread of a known
    # incumbent shows the hold — here Ohio (roster R, running) is priced 10% R, a
    # gross misread, so it shows the incumbent-hold 100.
    rows = [
        _lean_row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.10, None),
        # Georgia: a traded race (positive liquidity) → its market price stands.
        _lean_row(2, "Will the Republicans win the Georgia Senate race in 2026?", 0.55, 1000.0),
    ]
    result = aggregate(rows, _lean_ir(index_id="us-senate-2026-republican-seats"))
    ohio = result.breakdown["entities"]["Ohio"]
    assert ohio["no_market"] is True
    assert ohio["heat"] == pytest.approx(100.0)  # gross misread → incumbent-hold, not 10
    georgia = result.breakdown["entities"]["Georgia"]
    assert georgia["no_market"] is False
    assert georgia["heat"] == pytest.approx(55.0)
    # Top-line lean is the TRADED races only → Georgia's 55, Ohio (100) excluded.
    assert result.score == pytest.approx(55.0)


def test_entity_lean_mean_below_min_components_is_none() -> None:
    rows = [
        _lean_row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.80, 100.0)
    ]
    result = aggregate(rows, _lean_ir(min_components=2))
    assert result.score is None
    assert result.breakdown["reason"] == "below min_components"
