"""W4 — geo_weighted_mean: per-country directed PMI rolled country→region→world.

Builds ``MarketEvaluations`` with stub ``AuditEvaluation`` rows (country in the
title for the patterns tier) and asserts the leaf math matches Micah's legacy
country_index and the two-layer include_zero rollup behaves as designed.
Pure-Python, no DB / network.
"""

from __future__ import annotations

import json

import pytest

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    EntityRollupSpec,
    ExtractionSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
)
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.engine.geo import gdp_pop_weights, region_weights
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir(
    *,
    group_through: str | None = "region",
    include_zero: bool = False,
    methods: list[str] | None = None,
) -> IndexDef:
    methods = methods or ["patterns"]
    factors = [
        FactorSpec(id="about_war", type="binary", prompt_ref="prompts/factors/about-war-v1", weight=1.0),
        FactorSpec(id="direction", type="ternary", prompt_ref="prompts/factors/direction-v1", weight=None),
    ]
    if "llm_fallback" in methods:
        factors.append(
            FactorSpec(id="country_x", type="extraction", prompt_ref="prompts/factors/country-extraction-v1", weight=None)
        )
    return IndexDef(
        id="war-test",
        version=1,
        title="War Test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["war"])],
        factors=factors,
        extraction=ExtractionSpec(group_by="country", sides="none", methods=methods),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False),
            min_components=1,
            formula="geo_weighted_mean",
            rollup=EntityRollupSpec(
                cross_entity="gdp_pop_weighted",
                group_through=group_through,
                include_zero_groups=include_zero,
            ),
        ),
    )


def _row(
    market_id: int,
    *,
    title: str,
    last_price: float,
    direction: float,
    relevancy: float = 1.0,
    extraction: tuple[str, str] | None = None,
) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id, venue="polymarket", external_id=f"ext-{market_id}", slug=None, title=title
    )
    market.is_play_money = False

    def _ev(factor_id: str, value: float, label: str | None = None) -> AuditEvaluation:
        return AuditEvaluation(
            market_id=market_id, index_definition_id=1, factor_id=factor_id,
            prompt_id=1, prompt_sha256="x" * 64, model_id="stub",
            value_numeric=value, value_label=label,
        )

    by_factor = {
        "about_war": _ev("about_war", relevancy),
        "direction": _ev("direction", direction),
    }
    if extraction is not None:
        # value=1 + canonical {"country": slug} label, as llm/base.py would store.
        by_factor["country_x"] = _ev("country_x", 1.0, json.dumps({"country": extraction[1]}))
    return MarketEvaluations(
        market=market, by_factor=by_factor, last_price=last_price, liquidity=None
    )


def _entities(result):
    return result.breakdown["entities"]


def test_leaf_escalation_heat_is_price_x100():
    rows = [_row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0)]
    r = aggregate(rows, _ir())
    assert _entities(r)["ukraine"]["heat"] == 80.0


def test_leaf_peace_flips_price():
    # A peace market (direction -1) contributes 1 - price.
    rows = [_row(1, title="Will there be a ceasefire in Ukraine in 2026?", last_price=0.9, direction=-1.0)]
    r = aggregate(rows, _ir())
    assert _entities(r)["ukraine"]["heat"] == pytest.approx(10.0)


def test_leaf_neutral_excluded():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will diplomats meet about Ukraine?", last_price=0.5, direction=0.0),
    ]
    r = aggregate(rows, _ir())
    assert _entities(r)["ukraine"]["n_markets"] == 1
    assert _entities(r)["ukraine"]["heat"] == 80.0


def test_leaf_weighted_mean_multiple_markets():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will fighting in Ukraine continue?", last_price=0.6, direction=1.0),
        _row(3, title="Will there be a ceasefire in Ukraine?", last_price=0.9, direction=-1.0),
    ]
    r = aggregate(rows, _ir())
    # equal weights (no liquidity) → (0.8 + 0.6 + 0.1)/3 × 100 = 50.0
    assert _entities(r)["ukraine"]["heat"] == pytest.approx(50.0)


def test_region_no_include_zero_matches_country_when_single():
    rows = [_row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0)]
    r = aggregate(rows, _ir(group_through="region", include_zero=False))
    groups = r.breakdown["groups"]
    assert groups["europe"]["heat"] == 80.0
    # Only one region scored → world == that region's heat.
    assert r.score == pytest.approx(80.0)


def test_include_zero_drags_region_down():
    # Ukraine 80, rest of Europe absent → region heat = 80 × ukraine's GDP/pop
    # share of the full European roster (≪ 80).
    rows = [_row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0)]
    r = aggregate(rows, _ir(group_through="region", include_zero=True))
    europe = r.breakdown["groups"]["europe"]
    assert 0.0 < europe["heat"] < 80.0
    assert europe["n_entities"] == 52  # full European roster
    assert europe["n_scored"] == 1


def test_world_excludes_zero_regions_and_weights_by_region():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will the Fed cut rates in 2026?", last_price=0.6, direction=1.0),
    ]
    # Market 2 names no country → patterns claims nothing, it contributes to no
    # region; only Ukraine (Europe) scores. World == Europe heat (single
    # scored region).
    r = aggregate(rows, _ir(group_through="region", include_zero=False))
    assert set(r.breakdown["groups"]) == {"europe"}
    assert r.score == pytest.approx(80.0)


def test_two_regions_world_is_region_weighted():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will there be war in Taiwan in 2026?", last_price=0.6, direction=1.0),
    ]
    r = aggregate(rows, _ir(group_through="region", include_zero=False))
    g = r.breakdown["groups"]
    assert g["europe"]["heat"] == 80.0
    assert g["asia"]["heat"] == 60.0
    rw = region_weights(["europe", "asia"])
    expected = (80.0 * rw["europe"] + 60.0 * rw["asia"]) / (rw["europe"] + rw["asia"])
    assert r.score == pytest.approx(expected, abs=1e-3)


def test_flat_world_is_gdp_pop_over_scored_countries():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will there be war in Taiwan in 2026?", last_price=0.6, direction=1.0),
    ]
    r = aggregate(rows, _ir(group_through=None, include_zero=False))
    assert "groups" not in r.breakdown
    w = gdp_pop_weights(["ukraine", "taiwan"])
    expected = (80.0 * w["ukraine"] + 60.0 * w["taiwan"]) / (w["ukraine"] + w["taiwan"])
    assert r.score == pytest.approx(expected, abs=1e-3)


def _ir_gated() -> IndexDef:
    """War IR whose on-topic binary `about_war` is a membership gate."""
    return IndexDef(
        id="war-gated", version=1, title="War Gated", owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["war"])],
        factors=[
            FactorSpec(id="about_war", type="binary", prompt_ref="p/about-war-v1", weight=30, gate=True),
            FactorSpec(id="armed", type="binary", prompt_ref="p/armed-v1", weight=70),
            FactorSpec(id="direction", type="ternary", prompt_ref="p/direction-v1", weight=None),
        ],
        extraction=ExtractionSpec(group_by="country", sides="none", methods=["patterns"]),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False), min_components=1, formula="geo_weighted_mean",
            rollup=EntityRollupSpec(cross_entity="gdp_pop_weighted", group_through="region", include_zero_groups=False),
        ),
    )


def _row_gated(market_id, *, title, last_price, about_war, armed, direction=1.0):
    market = CoreMarket(id=market_id, venue="polymarket", external_id=f"e{market_id}", slug=None, title=title)
    market.is_play_money = False

    def _ev(fid, v):
        return AuditEvaluation(market_id=market_id, index_definition_id=1, factor_id=fid,
                               prompt_id=1, prompt_sha256="x"*64, model_id="stub", value_numeric=v)
    return MarketEvaluations(
        market=market,
        by_factor={"about_war": _ev("about_war", about_war), "armed": _ev("armed", armed),
                   "direction": _ev("direction", direction)},
        last_price=last_price, liquidity=None,
    )


def test_gate_excludes_off_topic_market_that_trips_other_factors():
    # An esports-like market: about_war=0 (gate fails) but armed=1 → soft
    # relevancy 0.7 would otherwise keep it. The gate must drop it entirely.
    rows = [
        _row_gated(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, about_war=1.0, armed=1.0),
        _row_gated(2, title="Counter-Strike match in Taiwan", last_price=0.9, about_war=0.0, armed=1.0),
    ]
    r = aggregate(rows, _ir_gated())
    ents = _entities(r)
    assert "ukraine" in ents          # real war market kept
    assert "taiwan" not in ents       # gated out despite armed=1 (relevancy 0.7)


def test_gate_missing_eval_fails_closed():
    market = CoreMarket(id=9, venue="polymarket", external_id="e9", slug=None,
                        title="Will there be war in Ukraine in 2026?")
    market.is_play_money = False

    def _ev(fid, v):
        return AuditEvaluation(market_id=9, index_definition_id=1, factor_id=fid,
                               prompt_id=1, prompt_sha256="x"*64, model_id="stub", value_numeric=v)
    # No about_war eval at all → gate fails closed → excluded → no score.
    row = MarketEvaluations(
        market=market,
        by_factor={"armed": _ev("armed", 1.0), "direction": _ev("direction", 1.0)},
        last_price=0.8, liquidity=None,
    )
    r = aggregate([row], _ir_gated())
    assert r.score is None  # below min_components: the only market was gated out


def test_llm_fallback_classifies_bilateral():
    # Bilateral title patterns can't claim; the extraction factor picks Ukraine.
    rows = [
        _row(
            1,
            title="Will Russia invade Ukraine in 2026?",
            last_price=0.7,
            direction=1.0,
            extraction=("none", "ukraine"),
        )
    ]
    r = aggregate(rows, _ir(methods=["patterns", "llm_fallback"]))
    assert _entities(r)["ukraine"]["heat"] == 70.0
    assert _entities(r)["ukraine"]["contracts"][0]["venue"] == "polymarket"


# --------------------------------------------------------------------------
# 2026-10-12 — per-country relevancy floor (rollup.min_entity_relevancy)
# --------------------------------------------------------------------------


def _liq(row: MarketEvaluations, v: float) -> MarketEvaluations:
    row.liquidity = v
    return row


def _floor_rows(*, with_on_topic: bool = True) -> list[MarketEvaluations]:
    rows = [
        # four traded Ukraine markets so the quantile ladder applies (min sample 4)
        _liq(_row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0), 100.0),
        _liq(_row(2, title="Will fighting in Ukraine continue?", last_price=0.6, direction=1.0), 200.0),
        _liq(_row(3, title="Will Ukraine mobilize more troops?", last_price=0.7, direction=1.0), 300.0),
        _liq(_row(4, title="Will Ukraine hold elections during the war?", last_price=0.5, direction=1.0), 400.0),
        # Turkey: a traded LOW-relevancy sanctions market read inversely (8% → 92)
        _liq(_row(6, title="Will the U.S. lift CAATSA sanctions on Turkey?", last_price=0.08, direction=-1.0, relevancy=0.15), 84.0),
    ]
    if with_on_topic:
        # …and an on-topic Turkey market with no 24h liquidity (weight 0)
        rows.append(_liq(_row(5, title="Will Turkey launch a new military offensive by December 31?", last_price=0.035, direction=1.0, relevancy=0.8), 0.0))
    return rows


def _floor_ir(floor: float):
    ir = _ir()
    ir.weighting.liquidity.method = "quantile"
    ir.aggregation.rollup.min_entity_relevancy = floor
    return ir


def test_floor_off_lets_one_low_relevancy_contract_decide_a_country():
    r = aggregate(_floor_rows(), _floor_ir(0.0))
    t = _entities(r)["turkey"]
    assert t["heat"] == pytest.approx(92.0)
    assert t["weighting"] == "liquidity" and t["n_scoring"] == 1
    assert r.breakdown["rollup"]["min_entity_relevancy"] == 0.0


def test_floor_falls_back_to_relevancy_only_over_real_money_set():
    r = aggregate(_floor_rows(), _floor_ir(0.5))
    t = _entities(r)["turkey"]
    # (0.035·0.8 + 0.92·0.15) / 0.95
    assert t["heat"] == pytest.approx(17.47, abs=0.05)
    assert t["weighting"] == "relevancy_only" and t["n_scoring"] == 1
    assert {c["market_id"]: c["weight"] for c in t["contracts"]}[5] == 0.0
    # Ukraine, with plenty of traded relevancy, is untouched by the floor
    assert _entities(r)["ukraine"]["weighting"] == "liquidity"


def test_floor_unscores_a_country_with_no_relevancy_mass_at_all():
    r = aggregate(_floor_rows(with_on_topic=False), _floor_ir(0.5))
    assert "turkey" not in _entities(r)
    assert "ukraine" in _entities(r)
