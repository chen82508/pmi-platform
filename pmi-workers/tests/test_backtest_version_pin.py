"""SHIP-3.6 — pin an index version in the backtest + mirror each run to MLflow.

Two contracts:

1. ``score_index_as_of(index_id, as_of, version=...)`` replays the *pinned*
   definition, not just ``is_current`` — the prerequisite for replaying several
   versions over one window and diffing them.
2. The ``backtest`` workflow mirrors each run into MLflow as one comparable run
   (experiment ``pmi.<index_id>``; version / formula / yaml_sha256 tags; the
   per-point score curve as a stepped metric; summary metrics) — the whole
   comparison surface, no bespoke API/CSV.

DB-backed (the pmi-workers ephemeral Postgres harness): ``score_index_as_of``
and the workflow open real sessions and resolve factor models. MLflow itself is
monkeypatched — we assert *what would be logged*, not a live tracking server.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from pmi_core import mlflow_client
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
from pmi_core.workflow import create_run, execute_run
from sqlalchemy import text

INDEX_ID = "xver-pin-test"
# One weighted binary factor → relevancy 1.0 (value 1.0); no ternary → direction
# defaults to +1. With price 0.7 and no liquidity weighting the aggregate is
# 50 + 0.7*50 = 85.0. Real prompt file on disk (resolve_factor_models reads it).
_PROMPT = "prompts/factors/directly_about_war-v1"
_FACTOR_ID = "directly_about_war"
_EXPECTED_SCORE = 85.0


def _ir(version: int, min_components: int, formula: str = "weighted_average_x_100") -> IndexDef:
    return IndexDef(
        id=INDEX_ID,
        version=version,
        title="Version-pin test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["war"])],
        factors=[FactorSpec(id=_FACTOR_ID, type="binary", prompt_ref=_PROMPT, weight=30.0)],
        aggregation=AggregationSpec(
            formula=formula,
            collapse=CollapseSpec(enabled=False),
            min_components=min_components,
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method="none")),
    )


async def _seed_two_versions(
    session_factory, as_of: datetime, v2_min_components: int = 99
) -> tuple[int, int]:
    """v1 (min_components=1, historical) + v2 (current, caller-chosen min).

    Same factors, so the loader (index-independent, CORR-1.10) reuses the seeded
    evals for either version — the ONLY thing that differs is the aggregation
    gate. With the default ``v2_min_components=99`` v1 scores while v2 can't
    clear its own min (only 3 markets); pass ``1`` to make the current def
    scoreable too. Returns (v1_id, v2_id).
    """
    async with session_factory() as s:
        await s.execute(
            text(
                "TRUNCATE audit_evaluations, ts_price_snapshots, "
                "core_index_definitions, core_markets, core_prompts "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()

    ir_v1 = _ir(1, min_components=1)
    ir_v2 = _ir(2, min_components=v2_min_components)

    async with session_factory() as s:
        markets = [
            CoreMarket(venue="polymarket", external_id=f"pin-m{i}", title=f"War market {i}")
            for i in range(3)
        ]
        s.add_all(markets)
        await s.flush()
        market_ids = [m.id for m in markets]

        # Registry empty → resolves to (yaml prompt sha, stub model id); seed evals
        # under those exact keys so the loader matches them for either version.
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

        rm = resolved[_FACTOR_ID]
        for mid in market_ids:
            s.add(
                AuditEvaluation(
                    market_id=mid,
                    index_definition_id=v1.id,
                    factor_id=_FACTOR_ID,
                    prompt_id=rm.prompt.id,
                    prompt_sha256=rm.prompt.sha256,
                    model_id=rm.llm_model_id,
                    value_numeric=1.0,
                    confidence=0.9,
                    model_response={"stub": False},  # reusable (not a failed stub)
                    evaluated_at=datetime(2026, 6, 15, tzinfo=UTC),
                )
            )
            s.add(
                # Well before the earliest replay point so a multi-day backtest
                # (which walks BACKWARD from `as_of`) prices every point.
                TsPriceSnapshot(
                    market_id=mid,
                    snapshot_at=as_of - timedelta(days=10),
                    last_price=0.7,
                    volume_24h=1000.0,
                )
            )
        await s.commit()
        return v1.id, v2.id


async def test_score_index_as_of_pins_version(session_factory):
    """Pinning v1 scores; v2 (current) can't; omitting version follows current."""
    as_of = datetime(2026, 7, 1, tzinfo=UTC)
    await _seed_two_versions(session_factory, as_of)

    pinned_v1 = await score_index_as_of(INDEX_ID, as_of, version=1)
    assert pinned_v1["score"] == _EXPECTED_SCORE, pinned_v1
    assert pinned_v1["component_count"] == 3, pinned_v1
    assert pinned_v1["markets_priced"] == 3, pinned_v1

    # v2 has min_components=99 → below-min → None, off the SAME evals/prices.
    pinned_v2 = await score_index_as_of(INDEX_ID, as_of, version=2)
    assert pinned_v2["score"] is None, pinned_v2

    # No version → current (v2) → also None. Proves the default still tracks live.
    current = await score_index_as_of(INDEX_ID, as_of)
    assert current["score"] is None, current

    # A version that doesn't exist is a hard error, not a silent fallback.
    with pytest.raises(ValueError, match="no version 999"):
        await score_index_as_of(INDEX_ID, as_of, version=999)


class _MlflowCapture:
    """Records what the backtest would log; stands in for a tracking server."""

    def __init__(self) -> None:
        self.experiment: str | None = None
        self.tags: dict | None = None
        self.params: dict | None = None
        self.series: dict[str, list] = {}
        self.metrics: dict | None = None
        self.run_id = "run-xyz"
        self.experiment_id = "exp-1"

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(mlflow_client, "ensure_experiment", self._ensure)
        monkeypatch.setattr(mlflow_client, "start_run", self._start_run)
        monkeypatch.setattr(mlflow_client, "log_params", self._params)
        monkeypatch.setattr(mlflow_client, "log_metric_series", self._series)
        monkeypatch.setattr(mlflow_client, "log_metrics", self._metrics)
        # resolve_factor_models mirrors prompts into MLflow too — stub it so the
        # test is hermetic (no real tracking server) and doesn't stall on it.
        monkeypatch.setattr(mlflow_client, "register_prompt", lambda *a, **k: None)

    def _ensure(self, index_id):
        self.experiment = index_id
        return self.experiment_id

    @contextlib.contextmanager
    def _start_run(self, experiment_id, run_name, tags=None, parent_run_id=None):
        self.tags = dict(tags or {})
        self.run_name = run_name
        yield self.run_id

    def _params(self, run_id, params):
        self.params = dict(params)

    def _series(self, run_id, key, points):
        self.series[key] = list(points)

    def _metrics(self, run_id, metrics):
        self.metrics = dict(metrics)


async def test_backtest_workflow_records_mlflow_run(session_factory, monkeypatch):
    """Running the backtest workflow mirrors one comparable MLflow run."""
    cap = _MlflowCapture()
    cap.install(monkeypatch)

    as_of = datetime(2026, 7, 2, tzinfo=UTC)
    # v2 (current) scoreable so every replay point yields a value → a real curve.
    await _seed_two_versions(session_factory, as_of, v2_min_components=1)

    # Two replay points over the same window (end pinned for determinism).
    async with session_factory() as s:
        run = await create_run(
            s,
            "backtest",
            {
                "index_id": INDEX_ID,
                "days": 2,
                "step_hours": 24,
                "end": as_of.isoformat(),
            },
        )
        await s.commit()
        run_id = run.id

    result = await execute_run(run_id)

    # Result carries the resolved version + the MLflow handles for the caller.
    assert result["version"] == 2, result
    assert result["mlflow_run_id"] == cap.run_id
    assert result["mlflow_experiment_id"] == cap.experiment_id
    assert result["n_points"] == 2 and result["n_scored"] == 2, result

    # Experiment is the shared per-index one; tags identify the version + formula.
    assert cap.experiment == INDEX_ID
    assert cap.tags["kind"] == "backtest"
    assert cap.tags["index_version"] == "2"
    assert cap.tags["formula"] == "weighted_average_x_100"
    assert cap.tags["yaml_sha256"] == "b" * 64

    # The per-point score curve is a stepped series: (value, step, ts_ms) × n_points.
    assert "score" in cap.series
    score_series = cap.series["score"]
    assert [step for _v, step, _ts in score_series] == [0, 1]
    assert all(v == _EXPECTED_SCORE for v, _s, _ts in score_series), score_series
    assert all(isinstance(ts, int) for _v, _s, ts in score_series)

    # Summary metrics for the run-level compare columns.
    assert cap.metrics["n_points"] == 2
    assert cap.metrics["n_scored"] == 2
    assert cap.metrics["coverage"] == 1.0
    assert cap.metrics["score_mean"] == _EXPECTED_SCORE

    # Breakdown-level metrics ride along generically. This weighted_average
    # index surfaces `raw` (= numerator/denominator = 0.7); the SAME mechanism
    # is what lets a seat index surface p_r_control / stdev_r_seats so a
    # correlation-only version bump diverges in MLflow even when score doesn't.
    assert "raw" in cap.series, cap.series.keys()
    assert [step for _v, step, _ts in cap.series["raw"]] == [0, 1]
    assert all(round(v, 4) == 0.7 for v, _s, _ts in cap.series["raw"]), cap.series["raw"]
    assert round(cap.metrics["raw_last"], 4) == 0.7
