"""Ensemble provider (CORR-5.9) — multi-model voting for one factor eval.

``model_id`` format::

    ensemble/<member>+<member>[+<member>...]

where each member is itself a full provider-routable model id (``ollama/llama3.2``,
``gpt-4o-mini-2024-07-18``, ``local/...``). Members run CONCURRENTLY via their own
providers; the votes combine by factor type:

* **binary**  — majority on value (tie → the highest-confidence member wins)
* **ternary** — mode of the label (tie → highest confidence)
* **score**   — mean of values

Combined ``confidence = agreement_ratio × mean(confidence of agreeing members)``
so a 2-of-3 split is visibly less certain than 3-of-3. ``cost_usd`` / tokens sum
across members. ``extras["ensemble"]`` carries every member's (model, value,
confidence, rationale) so `audit_evaluations.model_response` keeps the full
voting record — §9 traceability.

A member failure doesn't sink the vote (logged, skipped); ALL members failing
raises, and the evaluator's fallback-to-stub takes over as usual.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import structlog

from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import LLMResponse

log = structlog.get_logger(__name__)


def _parse_members(model_id: str) -> list[str]:
    spec = model_id.removeprefix("ensemble/")
    members = [m.strip() for m in spec.split("+") if m.strip()]
    if len(members) < 2:
        raise ValueError(
            f"ensemble model_id needs ≥2 '+'-joined members, got: {model_id!r}"
        )
    return members


class EnsembleProvider:
    """Async multi-model voting provider implementing the `LLMProvider` protocol."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.members = _parse_members(model_id)

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,
        tools_config: dict | None = None,
    ) -> LLMResponse:
        from pmi_core.llm.base import get_provider  # late: avoid import cycle

        async def _one(member: str) -> tuple[str, LLMResponse | None, str | None]:
            try:
                provider = get_provider(member)
                resp = await provider.evaluate(
                    rendered_prompt=rendered_prompt,
                    factor=factor,
                    temperature=temperature,
                    market=market,
                    tools_config=tools_config,
                )
                return member, resp, None
            except Exception as exc:  # noqa: BLE001 - one bad member ≠ no vote
                return member, None, f"{type(exc).__name__}: {exc}"

        results = await asyncio.gather(*[_one(m) for m in self.members])
        votes = [(m, r) for m, r, _ in results if r is not None]
        failures = {m: e for m, r, e in results if r is None}
        if failures:
            log.warning(
                "ensemble.member_failed",
                model_id=self.model_id,
                failures={k: v[:120] for k, v in failures.items()},
            )
        if not votes:
            raise RuntimeError(
                f"ensemble {self.model_id}: every member failed: {failures}"
            )

        value, label, confidence, winner = self._combine(factor, votes)
        agreement = self._agreement(factor, votes, value, label)

        return LLMResponse(
            model_id=self.model_id,
            value_numeric=value,
            value_label=label,
            confidence=confidence,
            rationale=winner.rationale,
            raw_text=winner.raw_text,
            raw_response={"ensemble_winner": winner.model_id},
            prompt_tokens=sum(r.prompt_tokens for _, r in votes),
            completion_tokens=sum(r.completion_tokens for _, r in votes),
            cost_usd=sum(r.cost_usd for _, r in votes),
            extras={
                "ensemble": {
                    "members": [
                        {
                            "model": m,
                            "value_numeric": r.value_numeric,
                            "value_label": r.value_label,
                            "confidence": r.confidence,
                            "rationale": (r.rationale or "")[:300],
                        }
                        for m, r in votes
                    ],
                    "failed": list(failures),
                    "agreement": agreement,
                }
            },
        )

    @staticmethod
    def _agreement(
        factor: FactorSpec,
        votes: list[tuple[str, LLMResponse]],
        value: float,
        label: str | None,
    ) -> float:
        if factor.type == "score":
            return 1.0  # mean has no discrete agreement notion
        agreeing = [
            r
            for _, r in votes
            if (factor.type == "binary" and (r.value_numeric >= 0.5) == (value >= 0.5))
            or (factor.type == "ternary" and r.value_label == label)
        ]
        return len(agreeing) / len(votes)

    @staticmethod
    def _combine(
        factor: FactorSpec, votes: list[tuple[str, LLMResponse]]
    ) -> tuple[float, str | None, float, LLMResponse]:
        """Return (value_numeric, value_label, confidence, winning member resp)."""
        by_conf = sorted(votes, key=lambda mv: mv[1].confidence, reverse=True)

        if factor.type == "binary":
            yes = [r for _, r in votes if r.value_numeric >= 0.5]
            no = [r for _, r in votes if r.value_numeric < 0.5]
            side = yes if len(yes) > len(no) else no if len(no) > len(yes) else None
            if side is None:  # tie → highest-confidence member decides
                winner = by_conf[0][1]
                side = yes if winner.value_numeric >= 0.5 else no
            value = 1.0 if side is yes else 0.0
            winner = max(side, key=lambda r: r.confidence)
            conf = (len(side) / len(votes)) * (sum(r.confidence for r in side) / len(side))
            return value, None, conf, winner

        if factor.type == "ternary":
            counts = Counter(r.value_label for _, r in votes)
            top_label, top_n = counts.most_common(1)[0]
            if list(counts.values()).count(top_n) > 1:  # tie → highest confidence
                top_label = by_conf[0][1].value_label
            side = [r for _, r in votes if r.value_label == top_label]
            winner = max(side, key=lambda r: r.confidence)
            conf = (len(side) / len(votes)) * (sum(r.confidence for r in side) / len(side))
            return winner.value_numeric, top_label, conf, winner

        # score → mean value, mean confidence, highest-confidence rationale
        mean_v = sum(r.value_numeric for _, r in votes) / len(votes)
        mean_c = sum(r.confidence for _, r in votes) / len(votes)
        return mean_v, None, mean_c, by_conf[0][1]
