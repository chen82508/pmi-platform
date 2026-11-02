"""Fixture provider — replays pre-labelled judgements instead of calling an LLM.

Model ids ``fixture/<name>`` route here. Each fixture market carries the answers
in its vendor payload (``core_markets.raw["judgements"]``, loaded by
``pmi-core seed``), keyed by factor id:

    "judgements": {
        "armed_conflict": 1,
        "war_direction": -1,
        "country_extraction": {"value": 1, "country": "Ukraine"}
    }

A scalar is shorthand for ``{"value": <scalar>}``. Factors the market does not
list answer ``0`` (not relevant / nothing extracted), which is what a real model
says about an off-topic market.

The answer goes through the same ``parse_factor_response`` contract as a real
model's JSON, so registry resolution, evaluation rows, the cache and every
aggregation formula run unchanged — only the network call is replaced. This is
what lets the demo stack and CI score indices with no API key and no GPU.
"""

from __future__ import annotations

import json
from typing import Any

from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import LLMResponse, parse_factor_response


def _answer_for(market: Any, factor_id: str) -> dict[str, Any]:
    raw = getattr(market, "raw", None) or {}
    judgements = raw.get("judgements") if isinstance(raw, dict) else None
    answer = (judgements or {}).get(factor_id, 0)
    if isinstance(answer, dict):
        return answer
    return {"value": answer}


class FixtureProvider:
    """``LLMProvider`` that answers from the market's fixture labels."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,
        tools_config: dict | None = None,
    ) -> LLMResponse:
        payload = {"confidence": 1.0, **_answer_for(market, factor.id)}
        raw_text = json.dumps(payload, sort_keys=True)
        value_numeric, value_label, confidence, rationale = parse_factor_response(
            raw_text, factor
        )
        return LLMResponse(
            model_id=self.model_id,
            value_numeric=value_numeric,
            value_label=value_label,
            confidence=confidence,
            rationale=rationale or "fixture judgement",
            raw_text=raw_text,
            raw_response={"fixture": True},
            cost_usd=0.0,
        )
