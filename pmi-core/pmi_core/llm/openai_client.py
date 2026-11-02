"""OpenAI implementation of `LLMProvider`.

Uses the official `openai>=1.0` SDK with the AsyncOpenAI client. Always
requests `response_format={"type": "json_object"}` so the model is forced to
emit parseable JSON (matches our prompt contract).

Retry policy
------------
We wrap the API call in `tenacity` with exponential backoff on transient
errors (429, 500, 502, 503, 504). The retry budget is intentionally small (3
attempts) so a flaky model doesn't silently inflate the per-tick latency
budget — the pipeline as a whole retries by re-running the next tick.

Cost accounting
---------------
A small per-model price table lives in `_PRICE_PER_M_TOKENS`. When a price
is unknown we still return a `cost_usd=0.0` so the field stays populated;
the eval row's `model_id` is enough to reconstruct cost downstream once the
table is patched.

Auth + endpoint
---------------
Reads `OPENAI_API_KEY` from `pmi_core.config.settings`. Failing fast with a
clear error message is preferable to a 401 deep inside the SDK.

When `PMI_LLM_BASE_URL` is set the same provider is pointed at an
OpenAI-compatible server (vLLM / Ollama / TGI / a future self-hosted ML
server) using `PMI_LLM_API_KEY` (falling back to `OPENAI_API_KEY`). The
`AsyncOpenAI` client is constructed once per (base_url, api_key) pair and
cached so we don't pay connection setup on every factor evaluation.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import structlog

from pmi_core.config import settings
from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import LLMResponse, parse_factor_response

log = structlog.get_logger(__name__)


@lru_cache(maxsize=8)
def _get_async_client(base_url: str, api_key: str) -> Any:
    """Return a cached `AsyncOpenAI` client for the given endpoint.

    Keyed on (base_url, api_key) so OpenAI-direct and a self-hosted endpoint
    can coexist in the same process. `base_url=""` means hit OpenAI's default.
    """
    from openai import AsyncOpenAI

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)

# USD per **million** tokens. Conservative published 2026-05 list prices —
# bump as needed. Models not in this table get cost_usd=0 (still recorded so
# downstream backfills can fill in retroactively from model_id + token counts).
_PRICE_PER_M_TOKENS: dict[str, tuple[float, float]] = {
    # model_id : (input $/M, output $/M)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


def _estimate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = _PRICE_PER_M_TOKENS.get(model_id)
    if pricing is None:
        # try prefix match (e.g. dated suffixes)
        for prefix, p in _PRICE_PER_M_TOKENS.items():
            if model_id.startswith(prefix):
                pricing = p
                break
    if pricing is None:
        return 0.0
    inp, out = pricing
    return prompt_tokens / 1_000_000 * inp + completion_tokens / 1_000_000 * out


class OpenAIProvider:
    """Async OpenAI client implementing the `LLMProvider` protocol."""

    def __init__(self, model_id: str) -> None:
        # Strip provider hint prefixes; the SDK / server wants the bare model id.
        # `openai/` => OpenAI direct, `local/` / `self-hosted/` => the endpoint
        # configured via PMI_LLM_BASE_URL (set by `get_provider`).
        self.model_id = (
            model_id.removeprefix("openai/")
            .removeprefix("self-hosted/")
            .removeprefix("local/")
        )
        # A self-hosted endpoint supplies its own auth (often a dummy token);
        # OpenAI-direct needs a real key. Resolve once here so failures are loud.
        self.base_url = settings.llm_base_url
        resolved_key = settings.llm_api_key or settings.openai_api_key
        if not self.base_url and not resolved_key:
            raise RuntimeError(
                "OPENAI_API_KEY is empty — set it in pmi_data_platform/.env "
                "(or set PMI_LLM_BASE_URL + PMI_LLM_API_KEY for a self-hosted "
                "endpoint) before promoting a CoreFactorModel onto a real LLM."
            )
        # The OpenAI SDK rejects an empty api_key even when talking to a local
        # server that ignores it, so fall back to a harmless sentinel token.
        self.api_key = resolved_key or "sk-local-no-auth"

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,  # noqa: ARG002 — Tier 2 context, ignored single-shot
        tools_config: dict | None = None,  # noqa: ARG002 — see AgenticProvider
    ) -> LLMResponse:
        # Lazy import so test environments without the SDK still importable.
        from openai import APIConnectionError, APIError, APIStatusError, RateLimitError
        from tenacity import (
            AsyncRetrying,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        client = _get_async_client(self.base_url, self.api_key)

        started = time.perf_counter()
        last_error: Exception | None = None
        completion: Any = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
            retry=retry_if_exception_type(
                (APIConnectionError, RateLimitError, APIStatusError, APIError)
            ),
            reraise=True,
        ):
            with attempt:
                completion = await client.chat.completions.create(
                    model=self.model_id,
                    temperature=temperature if temperature is not None else 0.1,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a careful prediction-market analyst. "
                                "Follow the user's instructions exactly. Return "
                                "ONLY a valid JSON object — no prose, no fences."
                            ),
                        },
                        {"role": "user", "content": rendered_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
        latency_ms = int((time.perf_counter() - started) * 1000)

        if completion is None:
            raise RuntimeError(f"OpenAI call exhausted retries: {last_error!r}")

        raw_text = (completion.choices[0].message.content or "").strip()
        value_numeric, value_label, confidence, rationale = parse_factor_response(
            raw_text, factor
        )

        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cost_usd = _estimate_cost(self.model_id, prompt_tokens, completion_tokens)

        # `.model_dump()` is available on pydantic-2-backed SDK responses.
        try:
            raw_response = completion.model_dump()  # type: ignore[union-attr]
        except Exception:
            raw_response = {"text": raw_text}

        log.debug(
            "openai.evaluate.done",
            model=self.model_id,
            factor=factor.id,
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
        )
