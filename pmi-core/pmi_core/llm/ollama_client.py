"""Ollama implementation of `LLMProvider`.

Ollama (https://ollama.com) runs open models locally and exposes an
OpenAI-compatible API at `<host>:11434/v1`, so we reuse the same `openai>=1.0`
AsyncOpenAI client transport as `OpenAIProvider` — only the endpoint and the
auth story differ. A dedicated provider (rather than overloading
`PMI_LLM_BASE_URL` + `local/<model>`) lets an Ollama worker coexist with
OpenAI-direct in the same process: promote a CoreFactorModel with an
`ollama/<model>` model_id and it routes here regardless of `PMI_LLM_BASE_URL`.

Differences vs OpenAIProvider
-----------------------------
* **Endpoint**: `settings.ollama_base_url` (default `http://localhost:11434/v1`,
  overridden to `http://ollama:11434/v1` inside docker compose).
* **Auth**: none. Ollama ignores the bearer token, but the OpenAI SDK rejects
  an empty `api_key`, so we pass a harmless sentinel.
* **Cost**: always `0.0` — local inference has no per-token billing. The
  `model_id` is still recorded so downstream can attribute compute if desired.

JSON contract
-------------
We request `response_format={"type": "json_object"}` (supported by Ollama's
OpenAI-compatible endpoint on recent versions). Even when a small model fails
to honour it strictly, `parse_factor_response` is best-effort — it pulls the
JSON object out of fenced or mid-text responses — so parsing stays robust.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import structlog

from pmi_core.config import settings
from pmi_core.dsl.ir import FactorSpec
from pmi_core.llm.base import LLMResponse, parse_factor_batch, parse_factor_response

log = structlog.get_logger(__name__)


@lru_cache(maxsize=4)
def _get_async_client(base_url: str) -> Any:
    """Return a cached `AsyncOpenAI` client pointed at an Ollama endpoint.

    Keyed on `base_url` so the connection setup is paid once per endpoint
    rather than per factor evaluation. Auth is a sentinel — Ollama ignores it.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=base_url, api_key="ollama-no-auth")


class OllamaProvider:
    """Async Ollama client implementing the `LLMProvider` protocol."""

    def __init__(self, model_id: str) -> None:
        # Strip the routing prefix; Ollama wants the bare model tag
        # (e.g. `llama3.1`, `qwen2.5:7b`, `mistral`).
        self.model_id = model_id.removeprefix("ollama/")
        self.base_url = settings.ollama_base_url
        if not self.base_url:
            raise RuntimeError(
                "PMI_OLLAMA_BASE_URL is empty — set it in pmi_data_platform/.env "
                "(default http://localhost:11434/v1, or http://ollama:11434/v1 "
                "inside docker compose) before promoting a CoreFactorModel onto "
                "an ollama/* model_id."
            )

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,  # noqa: ARG002 — Tier 2 context, ignored single-shot
        tools_config: dict | None = None,  # noqa: ARG002 — see AgenticProvider
    ) -> LLMResponse:
        # Lazy import so test environments without the SDK stay importable.
        from openai import APIConnectionError, APIError, APIStatusError, RateLimitError
        from tenacity import (
            AsyncRetrying,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        client = _get_async_client(self.base_url)

        started = time.perf_counter()
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
            raise RuntimeError("Ollama call exhausted retries with no response")

        raw_text = (completion.choices[0].message.content or "").strip()
        value_numeric, value_label, confidence, rationale = parse_factor_response(
            raw_text, factor
        )

        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        # `.model_dump()` is available on pydantic-2-backed SDK responses.
        try:
            raw_response = completion.model_dump()  # type: ignore[union-attr]
        except Exception:
            raw_response = {"text": raw_text}

        log.debug(
            "ollama.evaluate.done",
            model=self.model_id,
            factor=factor.id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
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
            cost_usd=0.0,  # local inference — no per-token billing
            latency_ms=latency_ms,
        )

    async def evaluate_batch(
        self,
        *,
        batch_prompt: str,
        factor: FactorSpec,
        n: int,
        temperature: float | None = None,
    ) -> list[LLMResponse]:
        """Evaluate N markets for ONE factor in a single call. Returns N aligned
        `LLMResponse`s (index = original market position).

        The rubric is sent once and only the per-market blocks repeat, so on a
        bandwidth-bound GPU this amortises prefill (~2x measured at n=8 on a
        T4). Raises `ParseError` on any misalignment so the caller can fall
        back to per-market calls — never returns a partial/misaligned batch.

        Token counts are split evenly across the n rows (approximate — the
        prompt is shared, so per-row attribution is inherently fuzzy; cost is
        $0 for local inference anyway).
        """
        from openai import APIConnectionError, APIError, APIStatusError, RateLimitError
        from tenacity import (
            AsyncRetrying,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        client = _get_async_client(self.base_url)
        started = time.perf_counter()
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
                        {"role": "user", "content": batch_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if completion is None:
            raise RuntimeError("Ollama batch call exhausted retries with no response")

        raw_text = (completion.choices[0].message.content or "").strip()
        # Raises ParseError on any misalignment → caller falls back to singles.
        parsed = parse_factor_batch(raw_text, factor, n)

        usage = getattr(completion, "usage", None)
        ptok = int(getattr(usage, "prompt_tokens", 0) or 0) // max(n, 1)
        ctok = int(getattr(usage, "completion_tokens", 0) or 0) // max(n, 1)

        out: list[LLMResponse] = []
        for i, (value_numeric, value_label, confidence, rationale) in enumerate(parsed):
            out.append(
                LLMResponse(
                    model_id=self.model_id,
                    value_numeric=value_numeric,
                    value_label=value_label,
                    confidence=confidence,
                    rationale=rationale,
                    raw_text=raw_text,
                    raw_response={"batched": True, "batch_size": n, "batch_index": i},
                    prompt_tokens=ptok,
                    completion_tokens=ctok,
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                )
            )
        return out
