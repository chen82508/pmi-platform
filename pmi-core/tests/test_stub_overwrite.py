"""A real judgment must be able to replace a stub row (2026-07-28).

`load_cached_evaluations` skips stub rows so the market retries next tick. That
retry only helps if its answer can actually land — under a plain
`ON CONFLICT DO NOTHING` the stub owned the cache_key forever, so every tick
re-paid the same LLM calls and discarded every result (measured on a live
maga-governor tick: 4,605 ollama calls, 36 rows written).

These assert the conflict clause `persist_evaluation` emits, which is the part
that decides whether the retry lands. The four runtime cases (fresh insert /
stub overwritten / real protected from real / stub cannot clobber real) are
Postgres `ON CONFLICT` semantics over that clause.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine.factor_evaluator import persist_evaluation
from pmi_core.engine.factor_resolver import ResolvedFactorModel
from pmi_core.llm import LLMResponse
from pmi_core.models import AuditEvaluation, CoreMarket, CorePrompt

FACTOR = FactorSpec(
    id="is_governor_race_2026",
    type="binary",
    prompt_ref="prompts/factors/is-governor-race-v1",
    weight=30,
)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _CapturingSession:
    """Records every statement and reports the insert as having written id=7."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(7)

    async def get(self, _entity, ident, **_kw):
        row = AuditEvaluation()
        row.id = ident
        return row


def _resolved() -> ResolvedFactorModel:
    prompt = CorePrompt(
        name="is-governor-race-v1",
        version=1,
        template="Title: {market_title}\nDescription: {market_description}",
        sha256="a" * 64,
    )
    prompt.id = 11
    return ResolvedFactorModel(
        prompt=prompt,
        llm_model_id="ollama/gemma3:4b",
        temperature=0.1,
        tools_config=None,
        source="registry",
        factor_model_id=None,
        mlflow_registered_model_name=None,
        mlflow_model_version=None,
    )


def _market() -> CoreMarket:
    m = CoreMarket(venue="polymarket", external_id="x", title="t", description="d")
    m.id = 42
    return m


async def _emitted_sql(*, llm_response, llm_error) -> str:
    session = _CapturingSession()
    await persist_evaluation(
        session,
        _market(),
        FACTOR,
        1,
        _resolved(),
        llm_response=llm_response,
        llm_error=llm_error,
    )
    insert_stmt = session.statements[0]
    return str(insert_stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_real_judgment_overwrites_a_stub_row():
    sql = await _emitted_sql(
        llm_response=LLMResponse(
            model_id="ollama/gemma3:4b",
            value_numeric=1.0,
            value_label=None,
            confidence=0.9,
            rationale="clearly a 2026 governor race",
            raw_text='{"value": 1}',
            raw_response={"value": 1},
        ),
        llm_error=None,
    )
    assert "DO UPDATE" in sql
    # Only when the row it collides with is a stub — a real judgment is never
    # overwritten, which is what keeps audit_evaluations append-only in spirit.
    assert "WHERE audit_evaluations.model_response ->> 'stub' = 'true'" in sql
    assert "ON CONSTRAINT uq_audit_evaluations__cache_key" in sql
    for col in ("value_numeric", "value_label", "confidence", "model_response"):
        assert f"{col} = excluded.{col}" in sql


@pytest.mark.asyncio
async def test_stub_fallback_never_overwrites_anything():
    sql = await _emitted_sql(llm_response=None, llm_error="connection refused")
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql
