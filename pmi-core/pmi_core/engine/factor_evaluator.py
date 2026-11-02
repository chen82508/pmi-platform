"""Factor evaluator.

Dispatches on the resolved model_id:

* `stub-*`     → deterministic in-process pseudo-eval (`_stub_score`). Used
                 when no `CoreFactorModel` is promoted for a factor, so the
                 pipeline keeps running with zero LLM cost.
* `gpt-*` /
  `openai/*`   → `pmi_core.llm.openai_client.OpenAIProvider` (real eval, cost
                 + tokens recorded into `audit_evaluations`).
* others       → looked up via `pmi_core.llm.get_provider(model_id)`. Adding a
                 new provider = registering a prefix there.

Contract:
  evaluate_factor(session, market, factor, index_def_id, resolved,
                  experiment_id=None, parent_run_id=None)
      -> tuple[AuditEvaluation, bool]

  Returns `(row, cache_hit)`. `cache_hit=True` means the row was returned
  from the existing `audit_evaluations` entry without any LLM call or new
  write this tick — the caller uses this to roll fresh cost / llm_calls
  into `audit_pipeline_runs` without double-counting cached spend.

  `resolved: ResolvedFactorModel` is produced by
  `factor_resolver.resolve_factor_model` and bundles
  (prompt, llm_model_id, temperature, tools_config, source).

Idempotency: if an AuditEvaluation with the same cache_key
(market_id, index_definition_id, factor_id, prompt_sha256, model_id) exists,
it's returned unchanged with `cache_hit=True`. Switching to a new
CoreFactorModel with a different llm_model_id automatically invalidates the
cache and runs fresh evaluations.

The one exception is a stub row (`model_response.stub`), which records a FAILED
eval rather than a judgment: a later real judgment overwrites it in place. Stub
rows are skipped by the cache loader so they retry, and without the overwrite
that retry could never land — the stub owned the cache_key forever and every
tick re-paid its LLM calls for nothing.

MLflow mirroring: each NEW evaluation opens an MLflow child run under the
caller's parent run and logs params + metrics, including the registry binding
(`factor_model_id`, `mlflow_registered_model_name`) when source='registry'.
This per-eval child run is gated by `settings.mlflow_factor_child_runs`
(default on) — turn it off for high-throughput scoring (T1); the
`audit_evaluations` row stays authoritative with `mlflow_run_id` NULL.

Concurrency (T1): the pipeline splits a factor eval into the pure-async LLM
call (`run_factor_llm`, no session, safe to `asyncio.gather`) and the
sequential DB+MLflow write (`persist_evaluation`). `evaluate_factor` below is
the one-at-a-time wrapper kept for the CLI / tests.

Failure mode: if the real LLM call raises (auth, parse, network exhaustion),
we fall back to the deterministic stub and tag the evaluation row's
`model_response.fallback_reason` so the audit trail captures why. The pipeline
never breaks because of a single factor's LLM hiccup.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core import mlflow_client
from pmi_core.config import settings
from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine.factor_resolver import (
    DEFAULT_STUB_MODEL_ID,  # re-export so legacy imports keep working
    ResolvedFactorModel,
)
from pmi_core.llm import LLMResponse, get_provider, render_prompt
from pmi_core.llm.base import split_market_template
from pmi_core.models import AuditEvaluation, CoreMarket

log = structlog.get_logger(__name__)

# Kept for backward compatibility with anything still importing this constant.
STUB_MODEL_ID = DEFAULT_STUB_MODEL_ID


def _stub_score(market: CoreMarket, factor: FactorSpec) -> tuple[float, str | None, float]:
    """Reproducible pseudo-evaluation seeded by (market.id, factor.id)."""
    seed = f"{market.id}:{factor.id}".encode()
    digest = hashlib.sha256(seed).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))

    confidence = 0.5 + rng.random() * 0.45

    if factor.type == "binary":
        value = 1.0 if rng.random() > 0.4 else 0.0
        return value, None, confidence
    if factor.type == "ternary":
        choice = rng.choice([-1.0, 0.0, 1.0])
        label = {-1.0: "-", 0.0: "0", 1.0: "+"}[choice]
        return choice, label, confidence
    if factor.type == "extraction":
        # A stub cannot extract structure; "nothing extracted" is the only
        # honest deterministic answer, and consumers treat it as a no-op.
        return 0.0, None, confidence
    return rng.random(), None, confidence  # score


def _is_stub(model_id: str) -> bool:
    return model_id.startswith("stub-") or model_id == DEFAULT_STUB_MODEL_ID


async def _run_real_llm(
    market: CoreMarket,
    factor: FactorSpec,
    resolved: ResolvedFactorModel,
) -> LLMResponse:
    """Render the prompt and dispatch to the provider matching `model_id`.

    `market` + `resolved.tools_config` are forwarded for Tier 2 (agentic)
    providers — single-shot providers accept and ignore them.

    PURE async HTTP — no DB session, no MLflow. This is the only part of a
    factor eval that is safe to run concurrently (`asyncio.gather`); the
    pipeline fans these out under a semaphore (T1), then persists results
    sequentially via `persist_evaluation`.
    """
    provider = get_provider(resolved.llm_model_id)
    rendered = render_prompt(resolved.prompt.template, market)
    return await provider.evaluate(
        rendered_prompt=rendered,
        factor=factor,
        temperature=resolved.temperature,
        market=market,
        tools_config=resolved.tools_config,
    )


# Public alias — the pipeline's concurrent stage imports this to run the LLM
# call without touching the session.
run_factor_llm = _run_real_llm


# Factor types whose single-object JSON contract maps cleanly to a batched
# array. `extraction` is excluded: its output carries variable per-market
# fields (state/party/...), which don't fit the uniform array item shape — it
# always falls back to per-market calls.
_BATCHABLE_TYPES = frozenset({"binary", "ternary", "score"})


def build_batch_prompt(template: str, markets: list[CoreMarket]) -> str | None:
    """Build a single prompt asking for ONE factor across N markets.

    Returns the prompt, or ``None`` if the template can't be split (no
    `{market_*}` placeholder) — caller then uses per-market calls. Sends the
    rubric once (`head`) + numbered per-market blocks, and asks for a
    ``{"results":[{"i",...}]}`` array that :func:`parse_factor_batch` validates.
    """
    split = split_market_template(template)
    if split is None:
        return None
    head, market_block, _tail = split
    parts = [
        head,
        "",
        f"Evaluate EACH of the {len(markets)} markets below INDEPENDENTLY, "
        "applying the instructions above to each one separately. Judge each "
        "market strictly on its OWN text — do NOT let the other markets in the "
        "list, or the topic of these instructions, bias you toward a positive "
        "answer. Most markets in a list are typically NEGATIVE for any given "
        "criterion; return the negative value unless THIS market clearly meets "
        "the rule.",
        'Return ONLY a JSON object of this exact shape: '
        '{"results": [{"i": <market index>, "value": <as instructed>, '
        '"confidence": <as instructed>, "reasoning": "<one sentence>"}, ...]} '
        f"with EXACTLY one entry per market and \"i\" matching the given index "
        f"(0..{len(markets) - 1}). Do not add or drop entries.",
        "",
    ]
    for i, m in enumerate(markets):
        parts.append(f"--- Market [{i}] ---")
        parts.append(render_prompt(market_block, m))
        parts.append("")
    return "\n".join(parts)


async def run_factor_llm_batch(
    markets: list[CoreMarket],
    factor: FactorSpec,
    resolved: ResolvedFactorModel,
) -> dict[int, LLMResponse]:
    """Evaluate one factor across many markets in a single LLM call.

    Returns ``{market_id: LLMResponse}``. PURE async HTTP — no DB session (same
    contract as :func:`_run_real_llm`), safe to run under the pipeline's gather.

    Falls back to per-market calls (still one network round-trip each, run
    concurrently) when batching isn't applicable or fails:
      * factor type not in `_BATCHABLE_TYPES`
      * provider has no `evaluate_batch` (agentic/ensemble/OpenAI today)
      * template can't be split
      * the batch response misaligns (`ParseError` from `parse_factor_batch`)
        or any transport error — we never persist a misaligned batch.
    """
    provider = get_provider(resolved.llm_model_id)

    async def _singles() -> dict[int, LLMResponse]:
        results = await asyncio.gather(
            *[_run_real_llm(m, factor, resolved) for m in markets]
        )
        return {m.id: r for m, r in zip(markets, results, strict=True)}

    if (
        len(markets) <= 1
        or factor.type not in _BATCHABLE_TYPES
        or not hasattr(provider, "evaluate_batch")
    ):
        return await _singles()

    batch_prompt = build_batch_prompt(resolved.prompt.template, markets)
    if batch_prompt is None:
        return await _singles()

    try:
        responses = await provider.evaluate_batch(
            batch_prompt=batch_prompt,
            factor=factor,
            n=len(markets),
            temperature=resolved.temperature,
        )
        return {m.id: r for m, r in zip(markets, responses, strict=True)}
    except Exception as exc:  # noqa: BLE001 - any batch failure → safe per-market path
        log.warning(
            "factor_eval.batch_fallback",
            factor_id=factor.id,
            model_id=resolved.llm_model_id,
            n=len(markets),
            error=str(exc)[:160],
        )
        return await _singles()


async def persist_evaluation(
    session: AsyncSession,
    market: CoreMarket,
    factor: FactorSpec,
    index_definition_id: int,
    resolved: ResolvedFactorModel,
    *,
    llm_response: LLMResponse | None,
    llm_error: str | None = None,
    latency_ms: int = 0,
    experiment_id: str | None = None,
    parent_run_id: str | None = None,
) -> tuple[AuditEvaluation, bool]:
    """Build the eval payload, mirror to MLflow, and INSERT it (sequential).

    Branches exactly like the legacy single-shot path:
      * stub model         → deterministic `_stub_score`
      * `llm_response`      → real eval payload
      * `llm_error` set     → fallback-to-stub, recording the reason

    DB + MLflow only — must be called serially on a single session (callers
    do the concurrent LLM fan-out BEFORE this). Returns `(row, cache_hit)`;
    `cache_hit=True` only when ON CONFLICT found a row another tick wrote.
    """
    prompt = resolved.prompt
    model_id = resolved.llm_model_id

    if _is_stub(model_id):
        value_numeric, value_label, confidence = _stub_score(market, factor)
        rationale = "deterministic stub at P0"
        cost_usd = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        model_response_payload: dict = {
            "stub": True,
            "rationale": rationale,
            "model_source": resolved.source,
        }
    elif llm_response is not None:
        value_numeric = llm_response.value_numeric
        value_label = llm_response.value_label
        confidence = llm_response.confidence
        rationale = llm_response.rationale
        cost_usd = llm_response.cost_usd
        prompt_tokens = llm_response.prompt_tokens
        completion_tokens = llm_response.completion_tokens
        model_response_payload = {
            "stub": False,
            "model_source": resolved.source,
            "rationale": rationale,
            "raw_text": llm_response.raw_text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "raw_response": llm_response.raw_response,
        }
        if llm_response.extras:
            model_response_payload["extras"] = llm_response.extras
    else:
        # LLM raised upstream (caller passed llm_error) → deterministic fallback.
        value_numeric, value_label, confidence = _stub_score(market, factor)
        rationale = f"LLM call failed; using stub. Reason: {(llm_error or '')[:200]}"
        cost_usd = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        model_response_payload = {
            "stub": True,
            "fallback_reason": llm_error,
            "model_source": resolved.source,
            "rationale": rationale,
            "intended_model_id": model_id,
        }

    run_tags: dict[str, str | int] = {
        "factor_id": factor.id,
        "factor_type": factor.type,
        "market_id": market.id,
        "model_id": model_id,
        "model_source": resolved.source,  # 'yaml' or 'registry'
        "prompt_name": prompt.name,
        "prompt_version": prompt.version,
        "prompt_uri": prompt.mlflow_prompt_uri or "",
        "real_llm": "true" if llm_response is not None else "false",
    }
    if llm_error:
        run_tags["fallback_reason"] = llm_error[:120]
    if resolved.factor_model_id is not None:
        run_tags["factor_model_id"] = resolved.factor_model_id
    if resolved.mlflow_registered_model_name:
        run_tags["mlflow_registered_model"] = resolved.mlflow_registered_model_name
    if resolved.mlflow_model_version:
        run_tags["mlflow_model_version"] = resolved.mlflow_model_version

    # MLflow child run is a non-authoritative mirror; gate it for throughput
    # (T1). When off, the audit_evaluations row is identical except mlflow_run_id
    # is NULL. The insert below runs regardless — it is the source of truth.
    child_run_id: str | None = None
    if settings.mlflow_factor_child_runs:
        run_name = f"{factor.id}:{market.id}"
        with mlflow_client.start_run(
            experiment_id=experiment_id,
            run_name=run_name,
            parent_run_id=parent_run_id,
            tags=run_tags,
        ) as child_run_id:
            mlflow_client.log_params(
                child_run_id,
                {
                    "factor_id": factor.id,
                    "factor_type": factor.type,
                    "market_id": market.id,
                    "market_title": (market.title or "")[:250],
                    "prompt_sha256": prompt.sha256,
                    "prompt_name": prompt.name,
                    "prompt_version": prompt.version,
                    "model_id": model_id,
                    "model_source": resolved.source,
                    "factor_model_id": resolved.factor_model_id or "",
                    "temperature": resolved.temperature
                    if resolved.temperature is not None
                    else "",
                },
            )
            mlflow_client.log_metrics(
                child_run_id,
                {
                    "value_numeric": value_numeric,
                    "confidence": confidence,
                    "latency_ms": latency_ms,
                    "cost_usd": cost_usd,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            )
            if value_label is not None:
                mlflow_client.set_tags(child_run_id, {"value_label": value_label})

    # CORR-3.11: atomic INSERT ... ON CONFLICT (see legacy note).
    base_stmt = pg_insert(AuditEvaluation).values(
        market_id=market.id,
        index_definition_id=index_definition_id,
        factor_id=factor.id,
        prompt_id=prompt.id,
        prompt_sha256=prompt.sha256,
        model_id=model_id,
        temperature=resolved.temperature,
        value_numeric=value_numeric,
        value_label=value_label,
        confidence=confidence,
        model_response=model_response_payload,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        mlflow_run_id=child_run_id,
        evaluated_at=datetime.now(UTC),
    )

    # A stub payload is a FAILED eval, not a judgment. The cache loader skips
    # stub rows so the market retries next tick — but under DO NOTHING the
    # retry's real answer could never land, because the stub already held the
    # cache_key. Every tick then re-paid the same LLM calls and threw the
    # results away (measured: 4,605 calls, 36 kept, on one maga-governor tick).
    # So a real judgment overwrites a stub; a stub never overwrites anything,
    # and a real judgment never overwrites another real judgment — append-only
    # still holds for everything that is actually a judgment.
    if model_response_payload.get("stub"):
        conflict_stmt = base_stmt.on_conflict_do_nothing(
            constraint="uq_audit_evaluations__cache_key"
        )
    else:
        conflict_stmt = base_stmt.on_conflict_do_update(
            constraint="uq_audit_evaluations__cache_key",
            set_={
                "prompt_id": base_stmt.excluded.prompt_id,
                "temperature": base_stmt.excluded.temperature,
                "value_numeric": base_stmt.excluded.value_numeric,
                "value_label": base_stmt.excluded.value_label,
                "confidence": base_stmt.excluded.confidence,
                "model_response": base_stmt.excluded.model_response,
                "cost_usd": base_stmt.excluded.cost_usd,
                "latency_ms": base_stmt.excluded.latency_ms,
                "mlflow_run_id": base_stmt.excluded.mlflow_run_id,
                "evaluated_at": base_stmt.excluded.evaluated_at,
            },
            where=text("audit_evaluations.model_response ->> 'stub' = 'true'"),
        )

    new_id = (
        await session.execute(conflict_stmt.returning(AuditEvaluation.id))
    ).scalar_one_or_none()

    if new_id is None:
        # Existing row is a real judgment (DO UPDATE's WHERE filtered it out),
        # or a concurrent tick committed it first. Re-read + treat as hit.
        existing = (
            await session.execute(
                select(AuditEvaluation).where(
                    AuditEvaluation.market_id == market.id,
                    AuditEvaluation.index_definition_id == index_definition_id,
                    AuditEvaluation.factor_id == factor.id,
                    AuditEvaluation.prompt_sha256 == prompt.sha256,
                    AuditEvaluation.model_id == model_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True
        raise RuntimeError(
            "audit_evaluations ON CONFLICT fired but the conflicting row was "
            f"not found on re-read (market={market.id}, factor={factor.id})"
        )

    # populate_existing: on the stub-overwrite path the row is already in the
    # identity map (the cache loader SELECTed it before skipping it), so a plain
    # get() would hand back the pre-UPDATE stub payload and the caller would
    # aggregate the value we just replaced.
    row = await session.get(AuditEvaluation, new_id, populate_existing=True)
    if row is None:  # pragma: no cover — inserted id must be fetchable
        raise RuntimeError(f"inserted audit_evaluation id={new_id} not retrievable")
    return row, False


async def evaluate_factor(
    session: AsyncSession,
    market: CoreMarket,
    factor: FactorSpec,
    index_definition_id: int,
    resolved: ResolvedFactorModel,
    *,
    experiment_id: str | None = None,
    parent_run_id: str | None = None,
) -> tuple[AuditEvaluation, bool]:
    """Single-shot path (CLI `score`, tests): cache → LLM → persist, serial.

    The pipeline's high-throughput path does NOT call this — it batch-loads the
    cache, fans the LLM calls out concurrently (`run_factor_llm`), then persists
    serially (`persist_evaluation`). This wrapper keeps that same logic for the
    one-at-a-time callers.
    """
    prompt = resolved.prompt
    model_id = resolved.llm_model_id

    cache_q = select(AuditEvaluation).where(
        AuditEvaluation.market_id == market.id,
        AuditEvaluation.index_definition_id == index_definition_id,
        AuditEvaluation.factor_id == factor.id,
        AuditEvaluation.prompt_sha256 == prompt.sha256,
        AuditEvaluation.model_id == model_id,
    )
    existing = (await session.execute(cache_q)).scalar_one_or_none()
    if existing is not None:
        return existing, True

    started = time.perf_counter()
    llm_response: LLMResponse | None = None
    llm_error: str | None = None
    if not _is_stub(model_id):
        try:
            llm_response = await run_factor_llm(market, factor, resolved)
        except Exception as exc:  # noqa: BLE001 - fallback-to-stub by design
            llm_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "factor_evaluator.llm_failed_fallback_to_stub",
                factor_id=factor.id,
                model_id=model_id,
                market_id=market.id,
                error=llm_error[:200],
            )
    latency_ms = int((time.perf_counter() - started) * 1000)
    return await persist_evaluation(
        session,
        market,
        factor,
        index_definition_id,
        resolved,
        llm_response=llm_response,
        llm_error=llm_error,
        latency_ms=latency_ms,
        experiment_id=experiment_id,
        parent_run_id=parent_run_id,
    )

