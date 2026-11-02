"""OpenAI Batch API jobs (CORR-5.3) — half-price nightly recompute.

Two jobs:

* ``llm-batch-submit`` — for every current index whose factors are bound to an
  OpenAI model (``gpt-*`` / ``openai/*``), collect the (market, factor) cache
  misses (same logic as the live pipeline's stage 1/2), render the same prompts
  the live path would, and submit one Batch per index. State + the
  custom_id → cache-key-ingredients map persist in ``core_llm_batches``.
* ``llm-batch-poll`` — drive submitted batches to completion: poll status,
  download the output file, parse each line with the SAME
  ``parse_factor_response`` the live OpenAIProvider uses, and persist through
  ``persist_evaluation`` (ON CONFLICT-safe: a live eval that raced the batch
  simply wins and the batch line counts as a cache hit). Batch pricing is 50%
  of live — ``cost_usd`` applies the 0.5 multiplier.

Ollama/local factors are skipped (Batch API is an OpenAI feature; local GPU is
already $0 — there's nothing to discount). With no eligible factors the submit
job logs and exits cleanly, so it's safe on an all-ollama deployment.

``PMI_BATCH_DRY_RUN=true`` builds and logs the request file without uploading
(also the only verifiable mode on a box without an OpenAI key).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.dsl.ir import FactorSpec, IndexDef
from pmi_core.engine.factor_evaluator import persist_evaluation
from pmi_core.engine.factor_resolver import ResolvedFactorModel
from pmi_core.engine.pipeline import load_cached_evaluations, resolve_factor_models
from pmi_core.engine.selector import select_markets
from pmi_core.llm import render_prompt
from pmi_core.llm.base import LLMResponse, parse_factor_response
from pmi_core.llm.openai_client import _estimate_cost, _get_async_client
from pmi_core.models import CoreIndexDefinition, CoreLlmBatch, CoreMarket, CorePrompt
from sqlalchemy import select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.llm_batch")

_SYSTEM = (
    "You are a careful prediction-market analyst. Follow the user's "
    "instructions exactly. Return ONLY a valid JSON object — no prose, no fences."
)

_BATCH_PRICE_MULTIPLIER = 0.5  # OpenAI Batch API is half the live price.


def _eligible(model_id: str) -> bool:
    return model_id.startswith(("gpt-", "openai/"))


def _bare_model(model_id: str) -> str:
    return model_id.removeprefix("openai/")


@register("llm-batch-submit", queue="batch")
async def submit() -> None:
    dry_run = settings.batch_dry_run
    if not dry_run and not settings.openai_api_key:
        log.error("llm_batch.no_api_key", hint="set OPENAI_API_KEY or PMI_BATCH_DRY_RUN=true")
        return

    async with session_scope() as session:
        defs = (
            await session.execute(
                select(CoreIndexDefinition).where(CoreIndexDefinition.is_current.is_(True))
            )
        ).scalars().all()

        for def_row in defs:
            ir = IndexDef.model_validate(def_row.definition)
            markets = await select_markets(session, ir)
            if not markets:
                continue
            by_id = {m.id: m for m in markets}

            _prompt_rows, resolved_models = await resolve_factor_models(session, ir)

            eligible_factors = [
                f for f in ir.factors if _eligible(resolved_models[f.id].llm_model_id)
            ]
            if not eligible_factors:
                log.info("llm_batch.no_eligible_factors", index_id=ir.id)
                continue

            # Index-independent cache (CORR-1.10): keyed on
            # (market, factor, prompt_sha256, model_id), NOT index_definition_id.
            existing = await load_cached_evaluations(
                session, ir, resolved_models, list(by_id)
            )
            todo = [
                (m, f)
                for m in markets
                for f in eligible_factors
                if (m.id, f.id) not in existing
            ]
            if not todo:
                log.info("llm_batch.nothing_to_do", index_id=ir.id)
                continue

            lines: list[str] = []
            meta: dict[str, dict] = {}
            for m, f in todo:
                resolved = resolved_models[f.id]
                custom_id = f"{def_row.id}|{m.id}|{f.id}"
                meta[custom_id] = {
                    "market_id": m.id,
                    "factor_id": f.id,
                    "factor_type": f.type,
                    "prompt_ref": f.prompt_ref,
                    "prompt_id": resolved.prompt.id,
                    "model_id": resolved.llm_model_id,
                    "temperature": resolved.temperature,
                    "factor_model_id": resolved.factor_model_id,
                }
                body = {
                    "model": _bare_model(resolved.llm_model_id),
                    "temperature": resolved.temperature if resolved.temperature is not None else 0.1,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": render_prompt(resolved.prompt.template, m)},
                    ],
                    "response_format": {"type": "json_object"},
                }
                lines.append(
                    json.dumps(
                        {
                            "custom_id": custom_id,
                            "method": "POST",
                            "url": "/v1/chat/completions",
                            "body": body,
                        }
                    )
                )

            payload = "\n".join(lines) + "\n"
            if dry_run:
                out = Path(f"/tmp/pmi-batch-{ir.id}.jsonl")
                out.write_text(payload, encoding="utf-8")
                log.info(
                    "llm_batch.dry_run",
                    index_id=ir.id,
                    requests=len(lines),
                    file=str(out),
                )
                continue

            client = _get_async_client("", settings.openai_api_key)
            file_obj = await client.files.create(
                file=(f"pmi-batch-{ir.id}.jsonl", payload.encode("utf-8")),
                purpose="batch",
            )
            batch = await client.batches.create(
                input_file_id=file_obj.id,
                endpoint="/v1/chat/completions",
                completion_window="24h",
                metadata={"index_id": ir.id},
            )
            row = CoreLlmBatch(
                provider="openai",
                batch_id=batch.id,
                input_file_id=file_obj.id,
                status="submitted",
                index_id=ir.id,
                index_definition_id=def_row.id,
                request_count=len(lines),
                request_meta=meta,
            )
            session.add(row)
            await session.flush()
            log.info(
                "llm_batch.submitted",
                index_id=ir.id,
                batch_id=batch.id,
                requests=len(lines),
            )


@register("llm-batch-poll")
async def poll() -> None:
    if not settings.openai_api_key:
        log.error("llm_batch.no_api_key")
        return
    client = _get_async_client("", settings.openai_api_key)

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CoreLlmBatch).where(
                    CoreLlmBatch.status.in_(["submitted", "in_progress", "completed"])
                )
            )
        ).scalars().all()
        if not rows:
            log.info("llm_batch.poll_nothing_pending")
            return

        for row in rows:
            if row.status in ("submitted", "in_progress"):
                batch = await client.batches.retrieve(row.batch_id)
                if batch.status in ("validating", "in_progress", "finalizing"):
                    row.status = "in_progress"
                    log.info("llm_batch.still_running", batch_id=row.batch_id, provider_status=batch.status)
                    continue
                if batch.status != "completed":
                    row.status = batch.status  # failed / expired / cancelled
                    row.error_message = str(getattr(batch, "errors", ""))[:1000]
                    log.warning("llm_batch.terminal", batch_id=row.batch_id, status=batch.status)
                    continue
                row.status = "completed"
                row.completed_at = datetime.now(UTC)
                row.output_file_id = batch.output_file_id

            # status == completed → ingest the output file.
            content = await client.files.content(row.output_file_id)
            text = content.text if hasattr(content, "text") else content.decode()
            ingested = 0
            for line in text.splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                meta = row.request_meta.get(rec["custom_id"])
                if meta is None:
                    log.warning("llm_batch.unknown_custom_id", custom_id=rec["custom_id"])
                    continue
                resp_body = (rec.get("response") or {}).get("body") or {}
                choices = resp_body.get("choices") or []
                if not choices:
                    log.warning("llm_batch.empty_choices", custom_id=rec["custom_id"])
                    continue
                raw_text = (choices[0].get("message") or {}).get("content") or ""
                factor = FactorSpec(
                    id=meta["factor_id"],
                    type=meta["factor_type"],
                    prompt_ref=meta["prompt_ref"],
                )
                value, label, confidence, rationale = parse_factor_response(
                    raw_text.strip(), factor
                )
                usage = resp_body.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                cost = _estimate_cost(_bare_model(meta["model_id"]), pt, ct) * _BATCH_PRICE_MULTIPLIER
                llm_response = LLMResponse(
                    model_id=meta["model_id"],
                    value_numeric=value,
                    value_label=label,
                    confidence=confidence,
                    rationale=rationale,
                    raw_text=raw_text,
                    raw_response={"batch_id": row.batch_id},
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    cost_usd=cost,
                    extras={"batch": True},
                )
                prompt_row = await session.get(CorePrompt, meta["prompt_id"])
                market = await session.get(CoreMarket, meta["market_id"])
                resolved = ResolvedFactorModel(
                    prompt=prompt_row,
                    llm_model_id=meta["model_id"],
                    temperature=meta["temperature"],
                    tools_config=None,
                    source="registry",
                    factor_model_id=meta.get("factor_model_id"),
                    mlflow_registered_model_name=None,
                    mlflow_model_version=None,
                )
                await persist_evaluation(
                    session,
                    market,
                    factor,
                    row.index_definition_id,
                    resolved,
                    llm_response=llm_response,
                    llm_error=None,
                    latency_ms=0,
                )
                ingested += 1
            row.ingested_count = ingested
            row.status = "ingested"
            row.ingested_at = datetime.now(UTC)
            log.info(
                "llm_batch.ingested",
                batch_id=row.batch_id,
                index_id=row.index_id,
                ingested=ingested,
                of=row.request_count,
            )
