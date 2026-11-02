"""CORR-1.10 — backtest replay reuses a prior version's cached evals.

Regression guard for the "index-independent eval cache" contract: bumping an
index to a new version (an aggregation-only change — same factors / prompts /
models) must let the backtest replay score off the PREVIOUS version's
``audit_evaluations`` instead of returning an empty backtest until every market
is re-judged. Before this fix, ``backtest._evaluations_by_market`` filtered on
``index_definition_id``, so the senate v10 bump found zero evals under its own
id and needed a manual 2331-row eval copy to make the board go live.

DB-backed (uses the pmi-workers ephemeral Postgres harness) because
``score_index_as_of`` opens a real session and resolves factor models.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
    LiquidityWeighting,
    WeightingSpec,
)
from pmi_core.engine.backtest import score_index_as_of
from pmi_core.engine.pipeline import resolve_factor_models
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    TsPriceSnapshot,
)
from sqlalchemy import func, select, text

INDEX_ID = "xver-reuse-test"
# Real prompt files on disk — resolve_factor_models reads them to compute sha.
_FACTORS = (
    ("directly_about_war", "binary", "prompts/factors/directly_about_war-v1", 30.0),
    ("direction", "ternary", "prompts/factors/direction-v1", None),
)


def _ir(version: int, min_components: int) -> IndexDef:
    """Two versions differ ONLY in version + min_components (an aggregation-only
    bump). Factors / prompts / models are byte-identical, so v1's evals are a
    valid cache for v2."""
    return IndexDef(
        id=INDEX_ID,
        version=version,
        title="Cross-version reuse test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["war"])],
        factors=[
            FactorSpec(id=fid, type=ftype, prompt_ref=ref, weight=w)
            for fid, ftype, ref, w in _FACTORS
        ],
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False), min_components=min_components
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method="none")),
    )


async def test_backtest_reuses_prior_version_evals(session_factory):
    as_of = datetime(2026, 7, 1, tzinfo=UTC)
    ir_v1 = _ir(1, min_components=1)
    ir_v2 = _ir(2, min_components=2)  # current def; owns ZERO evals

    # These tables aren't in the autouse truncate set — clear them for isolation.
    async with session_factory() as s:
        await s.execute(
            text(
                "TRUNCATE audit_evaluations, ts_price_snapshots, "
                "core_index_definitions, core_markets, core_prompts "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()

    async with session_factory() as s:
        markets = [
            CoreMarket(venue="polymarket", external_id=f"reuse-m{i}", title=f"War market {i}")
            for i in range(3)
        ]
        s.add_all(markets)
        await s.flush()
        market_ids = [m.id for m in markets]

        # Registry is empty → resolves to (yaml prompt sha, stub model id). The
        # cached evals below are written with these exact keys so the loader
        # matches them; the important part is the KEY match, not the model name.
        _prompts, resolved = await resolve_factor_models(s, ir_v2)

        v1 = CoreIndexDefinition(
            index_id=INDEX_ID,
            version=1,
            title=ir_v1.title,
            owner="test",
            definition=ir_v1.model_dump(mode="json"),
            yaml_source="x",
            yaml_sha256="a" * 64,
            is_current=False,
            effective_from=datetime(2026, 6, 1, tzinfo=UTC),
            effective_to=as_of,
        )
        v2 = CoreIndexDefinition(
            index_id=INDEX_ID,
            version=2,
            title=ir_v2.title,
            owner="test",
            definition=ir_v2.model_dump(mode="json"),
            yaml_source="x",
            yaml_sha256="b" * 64,
            is_current=True,
            effective_from=as_of,
        )
        s.add_all([v1, v2])
        await s.flush()

        # Cached evals live ONLY under v1. Match v2's resolved (prompt_sha, model).
        for mid in market_ids:
            for fid, _t, _r, _w in _FACTORS:
                rm = resolved[fid]
                s.add(
                    AuditEvaluation(
                        market_id=mid,
                        index_definition_id=v1.id,
                        factor_id=fid,
                        prompt_id=rm.prompt.id,
                        prompt_sha256=rm.prompt.sha256,
                        model_id=rm.llm_model_id,
                        value_numeric=1.0,
                        confidence=0.9,
                        model_response={"stub": False},  # not a failed stub → reusable
                        evaluated_at=datetime(2026, 6, 15, tzinfo=UTC),
                    )
                )
            s.add(
                TsPriceSnapshot(
                    market_id=mid,
                    snapshot_at=as_of - timedelta(hours=1),
                    last_price=0.7,
                    volume_24h=1000.0,
                )
            )
        await s.commit()
        v1_id, v2_id = v1.id, v2.id

    # Negative control: the CURRENT (v2) definition owns no evals of its own.
    async with session_factory() as s:
        n_v2 = (
            await s.execute(
                select(func.count())
                .select_from(AuditEvaluation)
                .where(AuditEvaluation.index_definition_id == v2_id)
            )
        ).scalar_one()
        n_v1 = (
            await s.execute(
                select(func.count())
                .select_from(AuditEvaluation)
                .where(AuditEvaluation.index_definition_id == v1_id)
            )
        ).scalar_one()
    assert n_v2 == 0, "v2 should own zero evals — the whole point of the test"
    assert n_v1 == 6, "3 markets × 2 factors judged under v1"

    # THE assertion: backtest of the current def still scores, by reusing v1's
    # evals across the version boundary. Old index-scoped lookup → score None.
    point = await score_index_as_of(INDEX_ID, as_of)
    assert point["score"] is not None, f"expected reuse to yield a score, got {point}"
    assert point["component_count"] == 3, point
    assert point["markets_priced"] == 3, point
