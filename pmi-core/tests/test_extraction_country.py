"""W2 — country extraction grammar (war vertical): patterns, llm_fallback, rollup."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pmi_core.dsl.ir import ExtractionSpec
from pmi_core.engine import extraction
from pmi_core.engine.extraction import (
    classify_entities,
    parse_country_direct,
    parse_country_extraction_label,
    rollup_parent,
)


def _spec(**kw) -> ExtractionSpec:
    base = {"group_by": "country", "sides": "none", "methods": ["patterns", "llm_fallback"]}
    base.update(kw)
    return ExtractionSpec(**base)


def test_patterns_tier_single_country():
    assert parse_country_direct("Will North Korea test a nuclear weapon in 2026?") == [
        ("none", "north-korea")
    ]


def test_patterns_tier_bilateral_attributes_both():
    # A war index is overwhelmingly bilateral — both participants are claimed
    # (in title order), not dropped. This is the core coverage fix.
    assert parse_country_direct("Will Russia invade Ukraine in 2026?") == [
        ("none", "russia"),
        ("none", "ukraine"),
    ]


def test_patterns_tier_no_country():
    assert parse_country_direct("Will the Fed cut rates in 2026?") == []


def test_patterns_tier_meeting_venue_dropped():
    # Leaders'-meeting venue stays dropped (the named country is the venue).
    assert parse_country_direct("Will Trump and Putin meet in Switzerland?") == []


def test_extraction_label_resolves_country():
    label = json.dumps({"country": "Ukraine"})
    assert parse_country_extraction_label(label) == ("none", "ukraine")
    # Already-slug form and alias form both resolve.
    assert parse_country_extraction_label(json.dumps({"country": "north-korea"})) == (
        "none",
        "north-korea",
    )
    assert parse_country_extraction_label(json.dumps({"country": "DRC"})) == (
        "none",
        "dr-congo",
    )


def test_extraction_label_rejects_unknown():
    assert parse_country_extraction_label(json.dumps({"country": "Atlantis"})) is None
    assert parse_country_extraction_label(None) is None
    assert parse_country_extraction_label("not json") is None


def test_classify_entities_patterns_tier():
    markets = [
        (1, "Will there be a ceasefire in Gaza by 2026?", 0.4),
        (2, "Will North Korea test a nuclear weapon in 2026?", 0.7),
        (3, "Will Russia invade Ukraine in 2026?", 0.3),  # bilateral → BOTH
        (4, "Will the Fed cut rates in 2026?", 0.5),  # no country
    ]
    obs = classify_entities(_spec(methods=["patterns"]), markets)
    # Bilateral market 3 yields one observation per participant.
    keyed = {(o.market_id, o.key) for o in obs}
    assert keyed == {(1, "gaza"), (2, "north-korea"), (3, "russia"), (3, "ukraine")}
    # Partyless sentinel; code is the slug.
    by_id = {o.market_id: o for o in obs if o.market_id in (1, 2)}
    assert by_id[1].party == "none"
    assert by_id[1].code == "gaza"
    assert by_id[2].tier == "patterns"


def test_classify_entities_llm_fallback_fills_uncovered():
    # A title the patterns tier can't place (names no country) falls to the
    # audited extraction factor — bilateral titles are now claimed by patterns,
    # so the llm_fallback tier only fills genuinely countryless markets.
    markets = [(5, "Will the ceasefire hold through 2026?", 0.3)]
    ext = {5: ("none", "ukraine")}
    obs = classify_entities(
        _spec(methods=["patterns", "llm_fallback"]),
        markets,
        extraction_by_market=ext,
    )
    assert len(obs) == 1
    assert obs[0].key == "ukraine"
    assert obs[0].tier == "llm_fallback"


def test_patterns_beats_llm_fallback():
    # A title the patterns tier claims is never relabeled by the LLM hint.
    markets = [(2, "Will North Korea test a nuclear weapon in 2026?", 0.7)]
    ext = {2: ("none", "south-korea")}
    obs = classify_entities(
        _spec(methods=["patterns", "llm_fallback"]),
        markets,
        extraction_by_market=ext,
    )
    assert obs[0].key == "north-korea"
    assert obs[0].tier == "patterns"


def test_rollup_parent_country_to_region():
    assert rollup_parent("country", "ukraine") == ("europe", "europe")
    assert rollup_parent("country", "taiwan") == ("asia", "asia")
    assert rollup_parent("country", "not-a-country") is None
    # State grammars stay flat.
    assert rollup_parent("us_state", "Ohio") is None


def test_country_grammar_registered():
    assert "country" in extraction._GRAMMARS


def test_spec_validator_country_requires_sides_none():
    with pytest.raises(ValidationError, match="requires sides 'none'"):
        ExtractionSpec(group_by="country", sides="party", methods=["patterns"])


def test_spec_validator_sides_none_forbids_candidates():
    with pytest.raises(ValidationError, match="incompatible with the 'candidates'"):
        ExtractionSpec(
            group_by="country", sides="none", methods=["patterns", "candidates"]
        )
