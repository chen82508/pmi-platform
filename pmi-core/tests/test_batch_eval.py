"""Unit tests for multi-market batch factor evaluation (2026-06-13).

Batching sends one factor's rubric ONCE + N market blocks in a single LLM
call (~2x throughput on a bandwidth-bound T4). These cover the pure layers —
no LLM/DB needed:
  * llm/base.split_market_template — rubric / per-market split
  * llm/base.parse_factor_batch    — aligned-array parse, fail-closed on drift
  * engine/factor_evaluator.build_batch_prompt — prompt assembly
  * engine/factor_evaluator.run_factor_llm_batch — fallback rules (fake provider)
  * engine/pipeline._batch_chunks  — k + char-budget packing
"""

from __future__ import annotations

import json

import pytest

from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine.factor_evaluator import build_batch_prompt, run_factor_llm_batch
from pmi_core.engine.factor_resolver import ResolvedFactorModel
from pmi_core.engine.pipeline import _batch_chunks
from pmi_core.llm.base import ParseError, parse_factor_batch, split_market_template
from pmi_core.models import CoreMarket

BINARY = FactorSpec(id="directly_about_war", type="binary",
                    prompt_ref="prompts/factors/directly_about_war-v1", weight=30)
TERNARY = FactorSpec(id="direction", type="ternary",
                     prompt_ref="prompts/factors/direction-v1", weight=None)
EXTRACTION = FactorSpec(id="seat_race_extraction", type="extraction",
                        prompt_ref="prompts/factors/seat-race-extraction-v1", weight=None)

TEMPLATE = (
    "You are judging whether a market is about war.\n"
    "Return JSON {\"value\": 0 or 1, \"confidence\": 0..1, \"reasoning\": \"...\"}.\n"
    "Mark value=1 only if directly about armed conflict.\n\n"
    "## Market\n"
    "Title: {market_title}\n"
    "Description: {market_description}\n\n"
    "Return the JSON only."
)


def _mkt(i: int, title: str = "t", desc: str = "d") -> CoreMarket:
    m = CoreMarket(venue="polymarket", external_id=str(i), title=title, description=desc)
    m.id = i
    return m


# ── split_market_template ──────────────────────────────────────────────────

def test_split_separates_rubric_from_market_block():
    head, block, tail = split_market_template(TEMPLATE)
    assert "{market_" not in head            # rubric carries no market data
    assert "judging whether a market" in head
    assert "{market_title}" in block and "{market_description}" in block
    assert "Return the JSON only" in tail


def test_split_returns_none_without_placeholder():
    assert split_market_template("no placeholders here") is None


# ── parse_factor_batch ───────────────────────────────────────────────────────

def _batch_json(items):
    return json.dumps({"results": items})


def test_parse_batch_aligned_binary():
    txt = _batch_json([
        {"i": 0, "value": 1, "confidence": 0.9},
        {"i": 1, "value": 0, "confidence": 0.8},
    ])
    out = parse_factor_batch(txt, BINARY, 2)
    assert [o[0] for o in out] == [1.0, 0.0]


def test_parse_batch_reorders_by_index():
    txt = _batch_json([
        {"i": 1, "value": 1, "confidence": 0.6},
        {"i": 0, "value": 0, "confidence": 0.6},
    ])
    out = parse_factor_batch(txt, BINARY, 2)
    assert out[0][0] == 0.0 and out[1][0] == 1.0   # placed by i, not arrival order


def test_parse_batch_ternary_labels():
    txt = _batch_json([{"i": 0, "value": -1, "confidence": 0.7}])
    out = parse_factor_batch(txt, TERNARY, 1)
    assert out[0][0] == -1.0 and out[0][1] == "-"


@pytest.mark.parametrize("items,n", [
    ([{"i": 0, "value": 1}], 2),                                  # too few
    ([{"i": 0, "value": 1}, {"i": 1, "value": 0}], 1),            # too many
    ([{"i": 0, "value": 1}, {"i": 0, "value": 0}], 2),            # duplicate index
    ([{"i": 0, "value": 1}, {"i": 5, "value": 0}], 2),            # out of range
    ([{"value": 1}, {"value": 0}], 2),                            # missing 'i'
])
def test_parse_batch_fails_closed_on_misalignment(items, n):
    with pytest.raises(ParseError):
        parse_factor_batch(_batch_json(items), BINARY, n)


def test_parse_batch_rejects_non_results():
    with pytest.raises(ParseError):
        parse_factor_batch(json.dumps({"value": 1}), BINARY, 1)


# ── build_batch_prompt ───────────────────────────────────────────────────────

def test_build_batch_prompt_amortises_rubric():
    markets = [_mkt(0, "War in X?"), _mkt(1, "Election in Y?"), _mkt(2, "Price of Z?")]
    p = build_batch_prompt(TEMPLATE, markets)
    assert p.count("judging whether a market") == 1     # rubric ONCE
    assert p.count("--- Market [") == 3                  # one block per market
    assert "War in X?" in p and "Election in Y?" in p
    assert '"results"' in p and "EXACTLY one entry per market" in p


def test_build_batch_prompt_none_when_unsplittable():
    assert build_batch_prompt("no placeholder rubric", [_mkt(0)]) is None


# ── _batch_chunks ────────────────────────────────────────────────────────────

def test_batch_chunks_respects_k():
    chunks = _batch_chunks([_mkt(i) for i in range(7)], k=3)
    assert [len(c) for c in chunks] == [3, 3, 1]


def test_batch_chunks_respects_char_budget():
    big = "x" * 5000
    chunks = _batch_chunks([_mkt(i, desc=big) for i in range(4)], k=8)
    # 8000-char budget → at most 1 big market (5000) per chunk before the next
    # would exceed; so each lands in its own chunk.
    assert all(len(c) == 1 for c in chunks) and len(chunks) == 4


# ── run_factor_llm_batch fallback rules (fake provider) ─────────────────────

class _FakeBatchProvider:
    """Records whether the batch path or single path was taken."""
    def __init__(self):
        self.batch_calls = 0
        self.single_calls = 0

    async def evaluate_batch(self, *, batch_prompt, factor, n, temperature=None):
        from pmi_core.llm.base import LLMResponse
        self.batch_calls += 1
        return [
            LLMResponse(model_id="fake", value_numeric=1.0, value_label=None,
                        confidence=0.9, rationale="r", raw_text="{}",
                        raw_response={"batched": True, "batch_index": i})
            for i in range(n)
        ]

    async def evaluate(self, *, rendered_prompt, factor, temperature=None, market=None, tools_config=None):
        from pmi_core.llm.base import LLMResponse
        self.single_calls += 1
        return LLMResponse(model_id="fake", value_numeric=0.0, value_label=None,
                           confidence=0.5, rationale=None, raw_text="{}", raw_response={})


def _resolved(prompt_template=TEMPLATE):
    class _P:
        template = prompt_template
        name = "factors/directly_about_war"
        version = 1
        sha256 = "x"
        mlflow_prompt_uri = None
    return ResolvedFactorModel(
        prompt=_P(), llm_model_id="ollama/fake", temperature=0.1, tools_config=None,
        source="registry", factor_model_id=1,
        mlflow_registered_model_name=None, mlflow_model_version=None,
    )


@pytest.mark.asyncio
async def test_batch_used_for_binary(monkeypatch):
    fake = _FakeBatchProvider()
    monkeypatch.setattr("pmi_core.engine.factor_evaluator.get_provider", lambda mid: fake)
    out = await run_factor_llm_batch([_mkt(0), _mkt(1), _mkt(2)], BINARY, _resolved())
    assert fake.batch_calls == 1 and fake.single_calls == 0
    assert set(out) == {0, 1, 2}


@pytest.mark.asyncio
async def test_extraction_factor_falls_back_to_singles(monkeypatch):
    fake = _FakeBatchProvider()
    monkeypatch.setattr("pmi_core.engine.factor_evaluator.get_provider", lambda mid: fake)
    out = await run_factor_llm_batch([_mkt(0), _mkt(1)], EXTRACTION, _resolved())
    assert fake.batch_calls == 0 and fake.single_calls == 2
    assert set(out) == {0, 1}


@pytest.mark.asyncio
async def test_single_market_skips_batch(monkeypatch):
    fake = _FakeBatchProvider()
    monkeypatch.setattr("pmi_core.engine.factor_evaluator.get_provider", lambda mid: fake)
    await run_factor_llm_batch([_mkt(0)], BINARY, _resolved())
    assert fake.batch_calls == 0 and fake.single_calls == 1
