"""W1 — geo resources: weights, region roster, alias matcher (war vertical)."""

from __future__ import annotations

import pytest

from pmi_core.engine import geo


def test_region_taxonomy_is_six_regions():
    assert [r["id"] for r in geo.REGIONS] == [
        "asia",
        "europe",
        "middle-east",
        "africa",
        "americas",
        "oceania",
    ]


def test_region_mapping_has_201_countries():
    assert len(geo.REGION_MAPPING) == 201


def test_weight_data_loads_201_countries():
    data = geo._weight_data()
    assert len(data) == 201
    gdp, pop = data["united-states"]
    assert gdp > 0 and pop > 0


def test_slug_and_name_roundtrip():
    assert geo.slugify_country("United States") == "united-states"
    assert geo.slugify_country("DR Congo") == "dr-congo"
    assert geo.country_name("dr-congo") == "DR Congo"
    assert geo.country_name("ukraine") == "Ukraine"


def test_region_of_and_roster():
    assert geo.region_of("ukraine") == "europe"
    assert geo.region_of("taiwan") == "asia"
    assert geo.region_of("gaza") == "middle-east"
    assert geo.region_of("not-a-country") is None
    roster = geo.countries_in_region("middle-east")
    assert "israel" in roster and "iran" in roster
    # Full roster sums back to 201 across the six regions.
    total = sum(len(geo.countries_in_region(r["id"])) for r in geo.REGIONS)
    assert total == 201


def test_gdp_pop_weights_normalize_within_group():
    w = geo.gdp_pop_weights(["united-states", "china", "russia"])
    assert set(w) == {"united-states", "china", "russia"}
    # 65/35 GDP/pop split → US (largest GDP) outweighs Russia.
    assert w["united-states"] > w["russia"]
    # Weights are shares within the group; they sum to ~1.0 when all have data.
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-9)


def test_gdp_pop_weights_missing_country_gets_average():
    # All 201 real slugs carry weight data, so use a synthetic slug to exercise
    # the legacy "no data → group-average weight" fallback branch.
    w = geo.gdp_pop_weights(["united-states", "china", "narnia"])
    assert "narnia" in w
    avg_of_known = (w["united-states"] + w["china"]) / 2
    assert w["narnia"] == pytest.approx(avg_of_known)


def test_gdp_pop_weights_all_missing_falls_back_to_equal():
    w = geo.gdp_pop_weights(["narnia", "atlantis"])
    assert w == {"narnia": 1.0, "atlantis": 1.0}


def test_match_country_single_unambiguous():
    assert geo.match_country("Will there be a ceasefire in Gaza by 2026?") == "gaza"
    assert geo.match_country("Will North Korea test a nuclear weapon in 2026?") == "north-korea"
    assert geo.match_country("Will the US strike targets in 2026?") == "united-states"


def test_match_country_bilateral_defers_to_llm():
    # Two countries → None (the patterns tier never guesses the primary).
    assert geo.match_country("Will Russia invade Ukraine in 2026?") is None
    assert geo.match_country("Will Israel strike Iran in 2026?") is None


def test_match_country_none_when_absent():
    assert geo.match_country("Will the Fed cut rates in 2026?") is None


def test_match_country_skips_meeting_venue():
    # A leaders' meeting names a country as the venue, not a war party — don't claim it.
    assert geo.match_country("Will Trump and Putin meet next in Australia?") is None
    assert geo.match_country("Will there be a Trump-Putin summit in Turkey?") is None
    assert geo.match_country("Will there be peace talks in Switzerland?") is None
    # A real single-country war market is still claimed.
    assert geo.match_country("Will there be war in Ukraine in 2026?") == "ukraine"


def test_countries_in_title_longest_first_no_substring_dupes():
    # "South Korea" must not also yield "Korea"; "Nigeria" must not yield "Niger".
    assert geo.countries_in_title("South Korea vs North Korea war") == [
        "south-korea",
        "north-korea",
    ]
    assert geo.countries_in_title("Conflict in Nigeria escalates") == ["nigeria"]
    assert geo.countries_in_title("Niger coup spreads") == ["niger"]


def test_alias_matching():
    assert geo.match_country("Will Britain join the conflict?") == "united-kingdom"
    assert geo.match_country("DPRK missile launch in 2026?") == "north-korea"
    assert geo.match_country("Fighting in the DRC worsens") == "dr-congo"


def test_match_countries_bilateral_attributes_both():
    # A bilateral war title attributes to every named country (many-to-many),
    # unlike match_country which defers >1 to the llm tier.
    assert geo.match_countries("Will Russia invade Ukraine in 2026?") == [
        "russia",
        "ukraine",
    ]


def test_match_countries_keeps_bilateral_peace_talks():
    # Regression: the meeting-venue guard must NOT drop two-party peace/ceasefire
    # markets — the countries are war parties, not the venue. "talks"/"summit"/
    # "peace deal" all previously triggered a false drop of real war markets.
    assert geo.match_countries("Formal peace talks between Russia and Ukraine?") == [
        "russia",
        "ukraine",
    ]
    assert geo.match_countries("Russia-Ukraine peace talks by December 31, 2026?") == [
        "russia",
        "ukraine",
    ]
    assert geo.match_countries("Will a Ukraine–Russia peace deal be announced?") == [
        "ukraine",
        "russia",
    ]


def test_match_countries_still_drops_single_country_venue():
    # A single named country under a meeting/summit is the venue — still dropped
    # (the leaders are people, not countries, so only the venue country matches).
    assert geo.match_countries("Will Putin and Zelenskyy meet next in Turkey?") == []
    assert geo.match_countries("Will there be a Trump-Putin summit in Australia?") == []
