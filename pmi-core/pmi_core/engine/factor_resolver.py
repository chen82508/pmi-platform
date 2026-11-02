"""Resolve the (prompt + LLM + temperature + tools) bundle for a factor at
evaluation time.

Lookup order:
    1. `CoreFactorModel(factor_id=..., is_active=True, stage='production')` — DB-promoted
    2. YAML default (factor.prompt_ref + STUB_MODEL_ID at P0 / real LLM later)

Yielding a `ResolvedFactorModel` keeps the evaluator a pure function — it
just consumes the bundle, never queries CoreFactorModel itself. Pipeline
calls this once per factor per tick (cheap) and re-uses across all markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.dsl.ir import FactorSpec
from pmi_core.models import CoreFactorModel, CorePrompt

log = structlog.get_logger(__name__)

# Sentinel for "no LLM bound yet — pipeline runs the deterministic stub".
# When real LLM lands (Sprint 2), the first CoreFactorModel row promoted to
# production replaces this default. Cache keys differ → fresh evaluations.
DEFAULT_STUB_MODEL_ID = "stub-deterministic-v1"


@dataclass(slots=True, frozen=True)
class ResolvedFactorModel:
    """The final (prompt, llm_model_id, temperature, tools) for one factor evaluation."""

    prompt: CorePrompt
    llm_model_id: str
    temperature: float | None
    tools_config: dict | None
    source: Literal["yaml", "registry"]
    factor_model_id: int | None  # core_factor_models.id, NULL when source='yaml'
    mlflow_registered_model_name: str | None
    mlflow_model_version: str | None


async def resolve_factor_model(
    session: AsyncSession,
    factor: FactorSpec,
    yaml_prompt: CorePrompt,
    *,
    stage: str = "production",
) -> ResolvedFactorModel:
    """Return the resolved model for `factor.id`.

    If a CoreFactorModel row is registered + active + at `stage`, it wins.
    Otherwise the YAML-side `yaml_prompt` + DEFAULT_STUB_MODEL_ID is used.
    """
    stmt = (
        select(CoreFactorModel, CorePrompt)
        .join(CorePrompt, CorePrompt.id == CoreFactorModel.prompt_id)
        .where(
            CoreFactorModel.factor_id == factor.id,
            CoreFactorModel.stage == stage,
            CoreFactorModel.is_active.is_(True),
        )
        .limit(1)
    )
    result = (await session.execute(stmt)).first()

    if result is None:
        # Loud, because this branch is indistinguishable from success everywhere
        # else: the score computes, the job goes green, the page renders. The
        # S&P index published a stub-decided membership for eight days after a
        # v1→v2 factor rename left `spx_*` rows in the registry and `sp500_*`
        # ids in the def — nothing anywhere said so. `models doctor` is the
        # active check; this is the passive one.
        log.warning(
            "factor_resolver.unbound_factor",
            factor_id=factor.id,
            stage=stage,
            falling_back_to=DEFAULT_STUB_MODEL_ID,
            hint="pmi-core models doctor",
        )
        return ResolvedFactorModel(
            prompt=yaml_prompt,
            llm_model_id=DEFAULT_STUB_MODEL_ID,
            temperature=None,
            tools_config=None,
            source="yaml",
            factor_model_id=None,
            mlflow_registered_model_name=None,
            mlflow_model_version=None,
        )

    fm, registry_prompt = result
    log.debug(
        "factor_resolver.registry_hit",
        factor_id=factor.id,
        factor_model_id=fm.id,
        version=fm.version,
        llm_model_id=fm.llm_model_id,
    )
    return ResolvedFactorModel(
        prompt=registry_prompt,
        llm_model_id=fm.llm_model_id,
        temperature=float(fm.temperature) if fm.temperature is not None else None,
        tools_config=fm.tools_config,
        source="registry",
        factor_model_id=fm.id,
        mlflow_registered_model_name=fm.mlflow_registered_model_name,
        mlflow_model_version=fm.mlflow_model_version,
    )
