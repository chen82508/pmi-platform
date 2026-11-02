"""The fixture provider replays a market's labelled judgements through the same
response contract a real model's JSON goes through."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import get_provider
from pmi_core.llm.fixture_client import FixtureProvider


def _factor(fid: str, ftype: str) -> FactorSpec:
    return FactorSpec(id=fid, type=ftype, prompt_ref=f"prompts/factors/{fid}-v1", weight=None)


MARKET = SimpleNamespace(
    raw={
        "judgements": {
            "armed_conflict": 1,
            "war_direction": -1,
            "country_extraction": {"value": 1, "country": "Ukraine"},
        }
    }
)


def test_routing_by_prefix() -> None:
    assert isinstance(get_provider("fixture/labels"), FixtureProvider)


async def _eval(market, factor: FactorSpec):
    return await FixtureProvider("fixture/labels").evaluate(
        rendered_prompt="ignored", factor=factor, market=market
    )


async def test_binary_and_ternary_answers() -> None:
    binary = await _eval(MARKET, _factor("armed_conflict", "binary"))
    assert (binary.value_numeric, binary.value_label) == (1.0, None)
    ternary = await _eval(MARKET, _factor("war_direction", "ternary"))
    assert (ternary.value_numeric, ternary.value_label) == (-1.0, "-")
    assert binary.cost_usd == 0.0
    assert binary.model_id == "fixture/labels"


async def test_extraction_answer_becomes_canonical_label() -> None:
    resp = await _eval(MARKET, _factor("country_extraction", "extraction"))
    assert resp.value_numeric == 1.0
    assert resp.value_label == '{"country": "Ukraine"}'


@pytest.mark.parametrize(
    "market",
    [MARKET, SimpleNamespace(raw={}), SimpleNamespace(raw=None), None],
)
async def test_unlabelled_factor_answers_zero(market) -> None:
    resp = await _eval(market, _factor("near_term", "binary"))
    assert resp.value_numeric == 0.0
