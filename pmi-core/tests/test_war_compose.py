"""W7 — war_compose: reshape a real geo_weighted_mean breakdown into cache payloads.

Feeds the ACTUAL aggregator output through war_compose so the aggregator →
war-snapshot → /war API field contract is locked (a breakdown-shape change that
would 500 the API fails here instead).
"""

from __future__ import annotations

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
from pmi_core.engine.war_compose import (
    compose_country_details,
    compose_national,
    compose_questions,
)
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir() -> IndexDef:
    return IndexDef(
        id="war-index-2026",
        version=1,
        title="War Index 2026",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["war"])],
        factors=[
            FactorSpec(id="about_war", type="binary", prompt_ref="prompts/factors/about-war-v1", weight=1.0),
            FactorSpec(id="direction", type="ternary", prompt_ref="prompts/factors/direction-v1", weight=None),
        ],
        extraction=ExtractionSpec(group_by="country", sides="none", methods=["patterns"]),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False),
            min_components=1,
            formula="geo_weighted_mean",
            rollup=EntityRollupSpec(
                cross_entity="gdp_pop_weighted", group_through="region", include_zero_groups=True
            ),
        ),
    )


def _row(market_id: int, *, title: str, last_price: float, direction: float) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id, venue="polymarket", external_id=f"ext-{market_id}", slug=f"slug-{market_id}", title=title
    )
    market.is_play_money = False

    def _ev(fid: str, v: float) -> AuditEvaluation:
        return AuditEvaluation(
            market_id=market_id, index_definition_id=1, factor_id=fid,
            prompt_id=1, prompt_sha256="x" * 64, model_id="stub", value_numeric=v,
        )

    return MarketEvaluations(
        market=market,
        by_factor={"about_war": _ev("about_war", 1.0), "direction": _ev("direction", direction)},
        last_price=last_price,
        liquidity=None,
    )


@pytest.fixture
def breakdown_and_score():
    rows = [
        _row(1, title="Will there be war in Ukraine in 2026?", last_price=0.8, direction=1.0),
        _row(2, title="Will there be war in Taiwan in 2026?", last_price=0.6, direction=1.0),
    ]
    result = aggregate(rows, _ir())
    return result.score, result.breakdown


def test_compose_national(breakdown_and_score):
    score, breakdown = breakdown_and_score
    national = compose_national(score, breakdown, 2026, "2026-06-25T00:00:00+00:00")
    assert national["world_heat"] == round(score, 2)
    assert national["n_countries"] == 2
    assert set(national["countries"]) == {"ukraine", "taiwan"}
    assert national["countries"]["ukraine"]["name"] == "Ukraine"
    assert national["countries"]["ukraine"]["region"] == "europe"
    assert national["countries"]["ukraine"]["heat"] == 80.0
    # Region rows present (europe + asia), with weight from the world fold.
    assert set(national["regions"]) == {"europe", "asia"}
    assert national["regions"]["europe"]["name"] == "Europe"
    assert national["regions"]["europe"]["n_scored"] == 1
    assert national["exchanges"] == ["polymarket"]
    assert national["n_markets"] == 2


def _enrich_breakdown() -> dict:
    # Two countries with priced contracts across two venues, one two-sided market
    # (id 10) attributed to both — for dedup + venue-split assertions.
    return {
        "entities": {
            "ukraine": {
                "name": "Ukraine", "region": "europe", "heat": 80.0, "n_markets": 2,
                "contracts": [
                    {"market_id": 10, "title": "RU×UA ceasefire?", "venue": "polymarket",
                     "yes_pct": 30.0, "volume_24h": 5000.0, "url": "u10"},
                    {"market_id": 11, "title": "UA NATO 2026?", "venue": "kalshi",
                     "yes_pct": 12.0, "volume_24h": 2000.0, "url": "u11"},
                ],
            },
            "russia": {
                "name": "Russia", "region": "europe", "heat": 40.0, "n_markets": 2,
                "contracts": [
                    {"market_id": 10, "title": "RU×UA ceasefire?", "venue": "polymarket",
                     "yes_pct": 30.0, "volume_24h": 5000.0, "url": "u10"},
                    {"market_id": 12, "title": "RU mobilization?", "venue": "polymarket",
                     "yes_pct": 55.0, "volume_24h": 9000.0, "url": "u12"},
                ],
            },
        }
    }


def test_compose_national_country_roster_enrichment():
    national = compose_national(50.0, _enrich_breakdown(), 2026, "2026-09-13T00:00:00+00:00")
    uk = national["countries"]["ukraine"]
    # Per-country 24h book = sum of its own contracts (5000 + 2000).
    assert uk["volume_24h"] == 7000.0
    assert uk["thin"] is True  # < $10K
    # Top contracts sorted by 24h volume, capped at 3, carrying only facing fields.
    assert [c["market_id"] for c in uk["top_contracts"]] == [10, 11]
    assert set(uk["top_contracts"][0]) == {"market_id", "title", "venue", "yes_pct", "volume_24h", "url", "play_money"}
    # World book dedupes the two-sided market 10 (5000+2000+9000, not +5000 twice).
    assert national["world_volume_24h"] == 16000.0
    # Venue split is by VOLUME, not count: polymarket 10+12 = 14000, kalshi 2000.
    assert national["venue_volume"] == {"polymarket": 14000.0, "kalshi": 2000.0}
    # Concentration is gated on ≥8 priced countries — only 2 here → None.
    assert national["concentration"] is None


def test_compose_national_concentration_is_a_proper_share():
    # 9 priced countries, one two-sided market (id 100) attributed to two of
    # them — the exact shape that made per-country volumes overlap the deduped
    # world total. The concentration share must still be ≤ 1.
    ents = {
        f"c{i}": {"name": f"C{i}", "region": "europe", "heat": 10.0 + i, "n_markets": 1,
                  "contracts": [{"market_id": i, "title": f"q{i}", "venue": "polymarket",
                                 "yes_pct": 20.0, "volume_24h": float((i + 1) * 1000), "url": f"u{i}"}]}
        for i in range(9)
    }
    # Two-sided market 100 in the two heaviest countries.
    for slug in ("c7", "c8"):
        ents[slug]["contracts"].append(
            {"market_id": 100, "title": "cross", "venue": "kalshi",
             "yes_pct": 40.0, "volume_24h": 50000.0, "url": "u100"})
    national = compose_national(30.0, {"entities": ents}, 2026, "2026-09-13T00:00:00+00:00")
    conc = national["concentration"]
    assert conc is not None and conc["n_countries_priced"] == 9
    assert 0.0 < conc["top4_share"] <= 1.0     # a real fraction, never >100%
    assert 0.0 <= conc["bottom30_share"] <= 1.0
    # World book dedupes market 100 (counted once, not once per country).
    per_country_sum = sum(c["volume_24h"] for c in national["countries"].values())
    assert national["world_volume_24h"] < per_country_sum


def test_compose_national_no_volume_is_none():
    # Fixture markets carry no volume_24h → roster volume fields degrade to None,
    # never 0 (0 would read as "priced at zero book").
    breakdown = {"entities": {"chad": {"name": "Chad", "heat": 10.0, "contracts": [
        {"market_id": 1, "title": "x", "venue": "polymarket", "yes_pct": 5.0}]}}}
    national = compose_national(10.0, breakdown, 2026, "2026-09-13T00:00:00+00:00")
    assert national["countries"]["chad"]["volume_24h"] is None
    assert national["countries"]["chad"]["thin"] is False  # unknown ≠ thin
    assert national["world_volume_24h"] is None


def test_compose_country_details_carry_contracts(breakdown_and_score):
    _score, breakdown = breakdown_and_score
    details = compose_country_details(breakdown, 2026)
    assert set(details) == {"ukraine", "taiwan"}
    uk = details["ukraine"]
    assert uk["heat"] == 80.0
    assert uk["region"] == "europe"
    assert len(uk["contracts"]) == 1
    c = uk["contracts"][0]
    assert c["market_id"] == 1
    assert c["venue"] == "polymarket"
    assert c["yes_pct"] == 80.0
    assert c["direction"] == 1


def test_compose_handles_empty_breakdown():
    national = compose_national(None, {}, 2026, "2026-06-25T00:00:00+00:00")
    assert national["world_heat"] is None
    assert national["countries"] == {}
    assert national["regions"] == {}
    assert compose_country_details(None, 2026) == {}


def test_compose_national_distinct_vs_attributions(breakdown_and_score):
    """A market attributed to several countries counts once in n_markets
    (distinct) and once per side in n_attributions."""
    score, breakdown = breakdown_and_score
    # Attribute Ukraine's market to Taiwan too (two-sided market shape).
    uk_contract = breakdown["entities"]["ukraine"]["contracts"][0]
    breakdown["entities"]["taiwan"]["contracts"].append(dict(uk_contract))
    breakdown["entities"]["taiwan"]["n_markets"] += 1

    national = compose_national(score, breakdown, 2026, "2026-06-25T00:00:00+00:00")
    assert national["n_markets"] == 2        # distinct market ids {1, 2}
    assert national["n_attributions"] == 3   # ukraine 1 + taiwan 2
    # Region cards: asia (taiwan) now holds both markets distinct.
    assert national["regions"]["asia"]["n_markets"] == 2
    assert national["regions"]["asia"]["n_attributions"] == 2


def test_compose_national_carries_version(breakdown_and_score):
    score, breakdown = breakdown_and_score
    national = compose_national(
        score, breakdown, 2026, "2026-06-25T00:00:00+00:00", version=12
    )
    assert national["version"] == 12


def test_compose_questions_total_and_truncated(breakdown_and_score):
    _score, breakdown = breakdown_and_score
    feed = compose_questions(breakdown, 2026)
    assert feed["total"] == 2
    assert feed["truncated"] is False
    assert len(feed["questions"]) == 2

    capped = compose_questions(breakdown, 2026, limit=1)
    assert capped["total"] == 2
    assert capped["truncated"] is True
    assert len(capped["questions"]) == 1
    # Highest relevancy·volume first — and a multi-country market appears once.
    assert capped["questions"][0]["market_id"] in {1, 2}


def _anchor_breakdown() -> dict:
    # Three contracts for Russia: two are the same "ceasefire" question (across
    # two venues), one is a distinct nuclear question.
    return {
        "entities": {
            "russia": {
                "name": "Russia",
                "heat": 40.0,
                "contracts": [
                    {"market_id": 1, "title": "Ceasefire by Oct?", "venue": "polymarket",
                     "yes_pct": 30.0, "direction": -1, "relevancy": 0.9, "volume_24h": 100.0, "url": "u1"},
                    {"market_id": 2, "title": "Ceasefire by Dec?", "venue": "manifold",
                     "yes_pct": 35.0, "direction": -1, "relevancy": 0.8, "volume_24h": 50.0, "url": "u2"},
                    {"market_id": 3, "title": "Nuclear test in 2026?", "venue": "manifold",
                     "yes_pct": 20.0, "direction": 1, "relevancy": 0.7, "volume_24h": 10.0, "url": "u3"},
                ],
            }
        }
    }


def test_compose_questions_folds_anchor():
    # markets 1+2 = one ceasefire anchor (cross-venue); market 3 = its own anchor.
    feed = compose_questions(_anchor_breakdown(), 2026, anchor_of={1: 1, 2: 1, 3: 3})
    assert feed["total"] == 2  # two distinct questions, not three contracts
    assert feed["truncated"] is False

    a0, a1 = feed["questions"]  # sorted by relevancy desc
    # Anchor is the highest-relevancy contract (market 1), folding 2 contracts
    # across both venues.
    assert a0["anchor_id"] == 1
    assert a0["market_id"] == 1
    assert a0["n_contracts"] == 2
    assert a0["venues"] == ["manifold", "polymarket"]
    assert {m["market_id"] for m in a0["members"]} == {1, 2}
    # The distinct nuclear question is a singleton anchor.
    assert a1["anchor_id"] == 3
    assert a1["n_contracts"] == 1
    assert a1["venues"] == ["manifold"]
    assert len(a1["members"]) == 1


def test_compose_questions_flat_when_no_anchor_map():
    # Backward compatible: no anchor_of → one entry per contract, no anchor fields.
    feed = compose_questions(_anchor_breakdown(), 2026)
    assert feed["total"] == 3
    assert all("anchor_id" not in q for q in feed["questions"])


def test_compose_questions_group_title_overrides_multi_country():
    # A multi-country anchor (gid 1) takes the supplied LLM title; a single-
    # country anchor (gid 3, absent from group_titles) keeps its contract title.
    feed = compose_questions(
        _anchor_breakdown(),
        2026,
        anchor_of={1: 1, 2: 1, 3: 3},
        group_titles={1: "Will Russia and Ukraine reach a ceasefire?"},
    )
    a0, a1 = feed["questions"]
    assert a0["anchor_id"] == 1
    assert a0["title"] == "Will Russia and Ukraine reach a ceasefire?"
    # The facing contract's own title survives in members for the drill-down.
    assert a0["members"][0]["title"] == "Ceasefire by Oct?"
    assert a1["anchor_id"] == 3
    assert a1["title"] == "Nuclear test in 2026?"  # untouched contract title


# --------------------------------------------------------------------------
# 2026-10-12 — real-money volume, live price refresh, facing titles
# --------------------------------------------------------------------------

from pmi_core.engine.war_compose import refresh_prices  # noqa: E402


def _turkey_breakdown(flagged: bool = True) -> dict:
    manifold = {"market_id": 1, "title": "100 military deaths in a conflict between Israel and Turkey before 2030?",
                "venue": "manifold", "yes_pct": 12.4, "direction": 1, "relevancy": 0.65,
                "volume_24h": 4899.98, "url": "https://manifold/1"}
    poly = {"market_id": 2, "title": "Will the U.S. lift CAATSA sanctions on Turkey by October 31, 2026?",
            "venue": "polymarket", "yes_pct": 8.0, "direction": -1, "relevancy": 0.15,
            "volume_24h": 83.84, "url": "https://poly/2"}
    if flagged:
        manifold["play_money"] = True
        poly["play_money"] = False
    return {
        "entities": {
            "turkey": {"code": "turkey", "name": "Turkey", "region": "middle-east", "heat": 17.5,
                       "n_markets": 2, "contracts": [manifold, poly]},
        },
        "groups": {"middle-east": {"code": "middle-east", "name": "Middle East", "heat": 17.5,
                                   "n_entities": 1, "n_scored": 1, "weight": 1.0}},
        "venues": {"manifold": 1, "polymarket": 1},
    }


@pytest.mark.parametrize("flagged", [True, False])
def test_volume_sums_are_real_money_only(flagged):
    nat = compose_national(10.0, _turkey_breakdown(flagged), 2026, "2026-10-12T00:00:00+00:00")
    assert nat["countries"]["turkey"]["volume_24h"] == 83.84
    assert nat["regions"]["middle-east"]["volume_24h"] == 83.84
    assert nat["world_volume_24h"] == 83.84
    # the venue split keeps every lane, labelled by venue
    assert nat["venue_volume"] == {"manifold": 4899.98, "polymarket": 83.84}
    top = nat["countries"]["turkey"]["top_contracts"]
    assert top[0]["market_id"] == 2 and top[0]["play_money"] is False
    assert top[1]["market_id"] == 1 and top[1]["play_money"] is True


def test_country_details_flag_play_money_and_copy_contracts():
    bd = _turkey_breakdown(flagged=False)
    details = compose_country_details(bd, 2026)
    rows = details["turkey"]["contracts"]
    assert [r["play_money"] for r in rows] == [True, False]
    rows[1]["yes_pct"] = 99.0
    assert bd["entities"]["turkey"]["contracts"][1]["yes_pct"] == 8.0  # breakdown untouched


def test_refresh_prices_updates_known_ids_only():
    rows = [{"market_id": 1, "yes_pct": 91.5}, {"market_id": 2, "yes_pct": 22.0}, {"market_id": None}]
    refresh_prices(rows, {1: 0.81})
    assert [r.get("yes_pct") for r in rows] == [81.0, 22.0, None]


def test_compose_questions_carries_facing_title_when_headline_differs():
    bd = {
        "entities": {
            "saudi-arabia": {"code": "saudi-arabia", "name": "Saudi Arabia", "region": "middle-east", "heat": 40.0,
                             "n_markets": 2, "contracts": [
                {"market_id": 10, "title": "Houthi x Saudi Arabia effective ceasefire by October 31?",
                 "venue": "polymarket", "yes_pct": 30.0, "direction": -1, "relevancy": 0.9, "volume_24h": 500.0, "url": "u10"},
                {"market_id": 11, "title": "Houthi x Saudi Arabia effective ceasefire by November 30?",
                 "venue": "polymarket", "yes_pct": 40.0, "direction": -1, "relevancy": 0.8, "volume_24h": 50.0, "url": "u11"},
            ]},
        },
        "groups": {},
    }
    out = compose_questions(bd, 2026, anchor_of={10: 10, 11: 10}, group_titles={10: "Will the Houthis and Saudi Arabia keep their ceasefire?"})
    q = out["questions"][0]
    assert q["title"] == "Will the Houthis and Saudi Arabia keep their ceasefire?"
    assert q["facing_title"] == "Houthi x Saudi Arabia effective ceasefire by October 31?"
    assert q["play_money"] is False
    # no headline → no facing_title key, the contract's own title stands
    out2 = compose_questions(bd, 2026, anchor_of={10: 10, 11: 10}, group_titles={})
    assert "facing_title" not in out2["questions"][0]
    assert out2["questions"][0]["title"] == "Houthi x Saudi Arabia effective ceasefire by October 31?"
