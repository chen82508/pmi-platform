"""Unit tests for the `extraction` factor type (seat-race LLM fallback).

Covers the three layers added 2026-06-12:
  * llm/base.parse_factor_response — generic extraction parsing (value 0/1 +
    canonical JSON value_label)
  * engine/seat_mapping.parse_extraction_label — domain validation (50-state
    map, party enum)
  * engine/factor_evaluator._stub_score — stub extracts nothing (no-op)
"""

from __future__ import annotations

import json

import pytest

from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine.factor_evaluator import _stub_score
from pmi_core.engine.seat_mapping import parse_extraction_label
from pmi_core.llm.base import ParseError, parse_factor_response
from pmi_core.models import CoreMarket

EXTRACTION_FACTOR = FactorSpec(
    id="seat_race_extraction",
    type="extraction",
    prompt_ref="prompts/factors/seat-race-extraction-v1",
    weight=None,
)


# --------------------------------------------------------------------------
# parse_factor_response — extraction type
# --------------------------------------------------------------------------


def test_extraction_parse_value_1_canonicalises_fields():
    text = json.dumps(
        {
            "value": 1,
            "state": "Ohio",
            "party": "R",
            "race_type": "party_direct",
            "confidence": 0.92,
            "reasoning": "party-direct senate race",
        }
    )
    v, label, confidence, rationale = parse_factor_response(text, EXTRACTION_FACTOR)
    assert v == 1.0
    assert confidence == pytest.approx(0.92)
    assert rationale == "party-direct senate race"
    # Label is canonical (sorted keys) and excludes the meta keys.
    assert json.loads(label) == {"state": "Ohio", "party": "R", "race_type": "party_direct"}
    assert label == json.dumps(
        {"party": "R", "race_type": "party_direct", "state": "Ohio"}, sort_keys=True,
        ensure_ascii=False,
    )


def test_extraction_parse_value_0_has_no_label():
    text = json.dumps({"value": 0, "confidence": 0.8, "reasoning": "a primary market"})
    v, label, _, _ = parse_factor_response(text, EXTRACTION_FACTOR)
    assert v == 0.0
    assert label is None


def test_extraction_parse_value_1_without_fields_raises():
    with pytest.raises(ParseError):
        parse_factor_response(json.dumps({"value": 1, "confidence": 0.9}), EXTRACTION_FACTOR)


def test_extraction_parse_non_numeric_value_raises():
    with pytest.raises(ParseError):
        parse_factor_response(
            json.dumps({"value": "yes", "state": "Ohio"}), EXTRACTION_FACTOR
        )


# --------------------------------------------------------------------------
# parse_extraction_label — domain validation
# --------------------------------------------------------------------------


def test_label_valid_state_and_party():
    label = json.dumps({"party": "D", "race_type": "candidate", "state": "New Hampshire"})
    assert parse_extraction_label(label) == ("D", "New Hampshire")


def test_label_normalises_state_case():
    assert parse_extraction_label(json.dumps({"party": "R", "state": "ohio"})) == ("R", "Ohio")


@pytest.mark.parametrize(
    "payload",
    [
        {"party": "R", "state": "Lima"},          # not a US state (hallucination guard)
        {"party": "I", "state": "Maine"},         # independents never resolve
        {"party": "Republican", "state": "Ohio"}, # party must be exactly R/D
        {"state": "Ohio"},                        # missing party
        {"party": "R"},                           # missing state
    ],
)
def test_label_rejects_invalid_payloads(payload):
    assert parse_extraction_label(json.dumps(payload)) is None


def test_label_rejects_garbage():
    assert parse_extraction_label(None) is None
    assert parse_extraction_label("") is None
    assert parse_extraction_label("not json") is None
    assert parse_extraction_label(json.dumps([1, 2])) is None


# --------------------------------------------------------------------------
# stub path — extraction stubs are a deterministic no-op
# --------------------------------------------------------------------------


def test_stub_extracts_nothing():
    market = CoreMarket(id=7, venue="polymarket", external_id="x", title="anything")
    value, label, confidence = _stub_score(market, EXTRACTION_FACTOR)
    assert value == 0.0
    assert label is None
    assert 0.0 <= confidence <= 1.0


# --------------------------------------------------------------------------
# IR guard — extraction factors must not carry relevancy weight
# --------------------------------------------------------------------------


def test_ir_rejects_weighted_extraction_factor():
    with pytest.raises(ValueError, match="weight must be null"):
        FactorSpec(
            id="seat_race_extraction",
            type="extraction",
            prompt_ref="prompts/factors/seat-race-extraction-v1",
            weight=10,
        )
