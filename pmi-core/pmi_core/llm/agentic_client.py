"""Tier 2 agentic provider — multi-step tool-calling factor evaluation.

This is the §6 "Deep Eval — Agentic" layer. Unlike Tier 1 (`OpenAIProvider`,
single shot prompt → JSON), the agent runs a *bounded* tool-calling loop so a
strong model can pull Polymarket depth signals (trade flow, orderbook, …)
before committing to a factor value, and emits a full reasoning/tool trace for
the audit row.

Triggering strategy
-------------------
* **Strategy A (now, registry-driven)**: promote a `CoreFactorModel` with
  ``model_id = "agentic/<base_model>"`` (e.g. ``agentic/gpt-4o``). Every market
  for that factor then runs the agent loop. No special evaluator logic — the
  resolver/evaluator already dispatch by ``llm_model_id``.
* **Strategy B (later, escalation)**: run Tier 1 first, then conditionally
  re-evaluate the hard cases (low confidence / cross-factor contradiction) on
  this provider. This provider is agnostic to *what* triggered it.

vs OpenAIProvider
-----------------
* Bounded tool loop (``max_steps``) instead of one call.
* ``LLMResponse.extras["trace"]`` carries the per-step reasoning + tool I/O so
  ``audit_evaluations.model_response.trace`` satisfies the §9 "every score is
  traceable" promise.
* ``cost_usd`` accumulates across *all* turns (loop calls + final JSON turn).

Transport
---------
Reuses `OpenAIProvider`'s cached AsyncOpenAI client + price table — the agent
talks to OpenAI-direct, or to the OpenAI-compatible endpoint configured via
``PMI_LLM_BASE_URL`` (vLLM / TGI / self-hosted). Anthropic (Sonnet/Opus, the
ultimate §6 target) is a future sibling provider; the loop here is transport
agnostic so swapping it in is a client-construction change only.

tools_config
------------
From ``CoreFactorModel.tools_config``. Shape::

    {"tools": ["recent_trades"], "max_steps": 4, "trade_lookback": 50}

* ``tools``         — which registered tools to expose (default: all).
* ``max_steps``     — tool-loop iteration cap (default ``_DEFAULT_MAX_STEPS``).
* ``trade_lookback``— default ``limit`` for the ``recent_trades`` tool.

``None`` / missing keys → defaults. Unknown tool names are dropped with a warning.

Adding a tool
-------------
Register a ``ToolDef`` in ``_TOOL_REGISTRY``: an OpenAI function schema + an
async executor ``(market, args) -> dict``. Each executor opens its *own*
read-only session via ``session_scope`` — it must never touch the in-flight
pipeline transaction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import LLMResponse, parse_factor_response
from pmi_core.llm.openai_client import _estimate_cost, _get_async_client
from pmi_core.models import TsTrade

log = structlog.get_logger(__name__)

_DEFAULT_MAX_STEPS = 4
_DEFAULT_TRADE_LOOKBACK = 50
_MAX_TRADE_LOOKBACK = 200

_SYSTEM_PROMPT = (
    "You are a careful prediction-market analyst evaluating ONE factor for ONE "
    "market. You may call tools to inspect live Polymarket signals (trade flow, "
    "etc.) for THIS market before deciding. Call a tool only when it would change "
    "your answer; otherwise reason directly. When you are done gathering evidence, "
    "stop calling tools and state your verdict in prose — you will then be asked "
    "for the final JSON."
)

_FINAL_DIRECTIVE = (
    "Now output ONLY the final JSON verdict for the factor — no prose, no fences. "
    "Use the exact JSON shape the original instructions specified "
    "({value, confidence, reasoning})."
)


# ─── Tool executors ──────────────────────────────────────────────────────────


async def _tool_recent_trades(market: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Summarise the most recent executed trades for the market under evaluation.

    Bound to ``market.id`` by the provider — the model cannot query other
    markets (it only ever evaluates one). Opens a dedicated read-only session.
    """
    limit = args.get("limit", _DEFAULT_TRADE_LOOKBACK)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _DEFAULT_TRADE_LOOKBACK
    limit = max(1, min(_MAX_TRADE_LOOKBACK, limit))

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    TsTrade.traded_at,
                    TsTrade.price,
                    TsTrade.size,
                    TsTrade.side,
                )
                .where(TsTrade.market_id == market.id)
                .order_by(TsTrade.traded_at.desc())
                .limit(limit)
            )
        ).all()

    if not rows:
        return {"trade_count": 0, "note": "no trades recorded for this market yet"}

    buy_vol = sum(float(s) for _t, _p, s, side in rows if side == "BUY")
    sell_vol = sum(float(s) for _t, _p, s, side in rows if side == "SELL")
    total_vol = buy_vol + sell_vol
    notional = sum(float(p) * float(s) for _t, p, s, _side in rows)
    vwap = (notional / total_vol) if total_vol else None

    return {
        "trade_count": len(rows),
        "window_newest": rows[0][0].isoformat(),
        "window_oldest": rows[-1][0].isoformat(),
        "last_price": float(rows[0][1]),
        "vwap": round(vwap, 6) if vwap is not None else None,
        "buy_volume": round(buy_vol, 4),
        "sell_volume": round(sell_vol, 4),
        "buy_sell_imbalance": (
            round((buy_vol - sell_vol) / total_vol, 4) if total_vol else None
        ),
    }


@dataclass(frozen=True, slots=True)
class ToolDef:
    """An agent tool: OpenAI function schema + async executor."""

    name: str
    schema: dict[str, Any]
    executor: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


_TOOL_REGISTRY: dict[str, ToolDef] = {
    "recent_trades": ToolDef(
        name="recent_trades",
        schema={
            "type": "function",
            "function": {
                "name": "recent_trades",
                "description": (
                    "Recent executed trades for the market under evaluation: "
                    "last price, VWAP, buy/sell volume and imbalance. Use to gauge "
                    "momentum and whether the market is actively traded before "
                    "judging the factor."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"How many recent trades to summarise "
                                f"(default {_DEFAULT_TRADE_LOOKBACK}, "
                                f"max {_MAX_TRADE_LOOKBACK})."
                            ),
                        }
                    },
                    "required": [],
                },
            },
        },
        executor=_tool_recent_trades,
    ),
}


def _select_tools(tools_config: dict | None) -> list[ToolDef]:
    """Resolve the enabled ToolDefs from a `tools_config`. None → all registered."""
    if not tools_config or "tools" not in tools_config:
        return list(_TOOL_REGISTRY.values())
    selected: list[ToolDef] = []
    for name in tools_config["tools"]:
        tool = _TOOL_REGISTRY.get(name)
        if tool is None:
            log.warning("agentic.unknown_tool", tool=name, known=list(_TOOL_REGISTRY))
            continue
        selected.append(tool)
    return selected


# ─── Provider ────────────────────────────────────────────────────────────────


class AgenticProvider:
    """Multi-step tool-calling provider implementing the `LLMProvider` protocol."""

    def __init__(self, model_id: str) -> None:
        # `agentic/gpt-4o` → base model `gpt-4o`. The base model must be one the
        # configured endpoint serves with tool-calling support.
        self.model_id = model_id.removeprefix("agentic/")
        self.base_url = settings.llm_base_url
        resolved_key = settings.llm_api_key or settings.openai_api_key
        if not self.base_url and not resolved_key:
            raise RuntimeError(
                "OPENAI_API_KEY is empty — set it in pmi_data_platform/.env "
                "(or PMI_LLM_BASE_URL + PMI_LLM_API_KEY) before promoting an "
                "agentic/* CoreFactorModel onto a real LLM."
            )
        self.api_key = resolved_key or "sk-local-no-auth"

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,
        tools_config: dict | None = None,
    ) -> LLMResponse:
        if market is None:
            raise RuntimeError(
                "AgenticProvider requires `market` context for its tools; "
                "the evaluator must pass it through. Did _run_real_llm forward it?"
            )

        from openai import APIConnectionError, APIError, APIStatusError, RateLimitError
        from tenacity import (
            AsyncRetrying,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        client = _get_async_client(self.base_url, self.api_key)
        temp = temperature if temperature is not None else 0.1
        max_steps = int((tools_config or {}).get("max_steps", _DEFAULT_MAX_STEPS))
        tools = _select_tools(tools_config)
        tool_specs = [t.schema for t in tools]
        tool_by_name = {t.name: t for t in tools}

        _retry = AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
            retry=retry_if_exception_type(
                (APIConnectionError, RateLimitError, APIStatusError, APIError)
            ),
            reraise=True,
        )

        async def _call(messages: list[dict], *, with_tools: bool, force_json: bool) -> Any:
            kwargs: dict[str, Any] = {
                "model": self.model_id,
                "temperature": temp,
                "messages": messages,
            }
            if with_tools and tool_specs:
                kwargs["tools"] = tool_specs
                kwargs["tool_choice"] = "auto"
            if force_json:
                kwargs["response_format"] = {"type": "json_object"}
            async for attempt in _retry:
                with attempt:
                    return await client.chat.completions.create(**kwargs)
            raise RuntimeError("unreachable")  # reraise=True guarantees an exception

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": rendered_prompt},
        ]
        trace: list[dict[str, Any]] = []
        prompt_tokens = 0
        completion_tokens = 0
        cost_usd = 0.0
        steps_used = 0
        started = time.perf_counter()

        def _accumulate(completion: Any) -> None:
            nonlocal prompt_tokens, completion_tokens, cost_usd
            usage = getattr(completion, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            prompt_tokens += pt
            completion_tokens += ct
            cost_usd += _estimate_cost(self.model_id, pt, ct)

        # ── Tool-calling loop ──────────────────────────────────────────────
        for step in range(max_steps):
            completion = await _call(messages, with_tools=True, force_json=False)
            _accumulate(completion)
            msg = completion.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                # Model has no more tools to call — its prose is the reasoning;
                # the final JSON is coerced below.
                trace.append({"step": step, "type": "reasoning", "content": msg.content})
                break

            steps_used = step + 1
            # Echo the assistant tool-call message back (required by the API).
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = tool_by_name.get(name)
                if tool is None:
                    result: dict[str, Any] = {"error": f"unknown tool {name!r}"}
                else:
                    try:
                        result = await tool.executor(market, args)
                    except Exception as exc:  # noqa: BLE001 — surface to the model, don't crash
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                trace.append(
                    {"step": step, "type": "tool", "tool": name, "args": args, "result": result}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

        # ── Forced final JSON turn ─────────────────────────────────────────
        messages.append({"role": "user", "content": _FINAL_DIRECTIVE})
        final = await _call(messages, with_tools=False, force_json=True)
        _accumulate(final)
        latency_ms = int((time.perf_counter() - started) * 1000)

        raw_text = (final.choices[0].message.content or "").strip()
        value_numeric, value_label, confidence, rationale = parse_factor_response(
            raw_text, factor
        )

        try:
            raw_response = final.model_dump()
        except Exception:
            raw_response = {"text": raw_text}

        log.info(
            "agentic.evaluate.done",
            model=self.model_id,
            factor=factor.id,
            market_id=market.id,
            tool_steps=steps_used,
            trace_len=len(trace),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

        return LLMResponse(
            model_id=self.model_id,
            value_numeric=value_numeric,
            value_label=value_label,
            confidence=confidence,
            rationale=rationale,
            raw_text=raw_text,
            raw_response=raw_response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            extras={
                "tier": 2,
                "tool_steps": steps_used,
                "tools_available": [t.name for t in tools],
                "trace": trace,
            },
        )
