"""Backtest workflow (CORR-8.1 flagship / SHIP-3.4) — replay an index over history.

Replays an index's score over the past N days using:

* the index's **current** definition, or a **pinned** ``version`` (IR from
  ``core_index_definitions``) — pinning is what lets several versions replay
  over the same window and be diffed in MLflow (SHIP-3.6); each backtest is
  mirrored to one MLflow run under the shared ``pmi.<index_id>`` experiment,
* the **append-only factor evaluations** already in ``audit_evaluations``
  (semantic verdicts like "is this about war" are price-independent, so the
  cached rows ARE the historical truth — no LLM is called, the replay is $0),
* per-day **historical prices**: the latest ``ts_price_snapshots`` row at or
  before each replay point (CORR-3.10's /prices-history backfill is what makes
  this dense).

Each replay day is one durable workflow step — a worker crash at day 60/90
resumes at day 61 on queue retry. Nothing is written to ``ts_index_scores``:
backtest output lives in ``core_workflow_runs.result`` so the live series
stays a pure record of real ticks.

Honesty constraints (surfaced in the result, not hidden):

* Markets with no price snapshot at-or-before a replay point are excluded for
  that day (they didn't observably exist yet) — NOT defaulted to 0.5.
* Liquidity weighting uses the as-of snapshot's ``volume_24h`` (orderbook
  depth history is too sparse pre-CORR-4.3 to replay honestly).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from pmi_core import mlflow_client
from pmi_core.db import session_scope
from pmi_core.dsl.ir import IndexDef
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.engine.pipeline import load_cached_evaluations, resolve_factor_models
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    TsPriceSnapshot,
)
from pmi_core.workflow import WorkflowContext, workflow

log = structlog.get_logger(__name__)

# Scalar breakdown fields worth replaying as their own MLflow series. A seat
# index's headline score is E[seats], which is correlation-INVARIANT — a
# ``seat_projection_sum`` version that only pins the copula ρ (senate v9→v10)
# leaves the score curve untouched and moves only the *distribution*: control
# probability and seat spread. Logging these makes that class of bump visible
# in the MLflow compare view instead of showing two identical score lines.
# Curated (not "every scalar") so static geometry echoes (holdover, total_seats)
# don't clutter the run; extend when a new outcome metric (e.g. Brier, SHIP-3.8)
# lands in a breakdown.
_BREAKDOWN_METRIC_KEYS = (
    "p_r_control",     # P(R controls chamber), tie-break included — ρ-sensitive
    "p_d_control",
    "p_r_majority",    # P(R strict majority) — ρ-sensitive
    "p_d_majority",
    "stdev_r_seats",   # seat-count spread — ρ-sensitive (senate 2.0→5.4)
    "n_contested",     # contested-seat count driving the projection
    "raw",             # weighted-average pre-scale ratio (non-seat indexes)
)


def _breakdown_metrics(breakdown: object) -> dict[str, float]:
    """The curated scalar subset of an aggregate ``breakdown`` for MLflow."""
    if not isinstance(breakdown, dict):
        return {}
    out: dict[str, float] = {}
    for key in _BREAKDOWN_METRIC_KEYS:
        v = breakdown.get(key)
        # bool is an int subclass; exclude it so a flag never logs as 0.0/1.0.
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
    return out


async def _load_def(
    session, index_id: str, version: int | None = None
) -> CoreIndexDefinition:
    """The definition row to replay against.

    ``version=None`` → the live (``is_current``) row, the historical-record
    default. A pinned ``version`` → that exact SCD2 row, which is what lets
    SHIP-3.6 replay several versions of one ``index_id`` over the same window
    and diff them in MLflow (a version bump that only changed the formula /
    weights still reuses the prior version's cached evals — CORR-1.10 — so the
    pinned replay is the pure effect of the aggregation change).
    """
    stmt = select(CoreIndexDefinition).where(CoreIndexDefinition.index_id == index_id)
    if version is not None:
        stmt = stmt.where(CoreIndexDefinition.version == version)
    else:
        stmt = stmt.where(CoreIndexDefinition.is_current.is_(True))
    row = (
        await session.execute(stmt.order_by(CoreIndexDefinition.version.desc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        what = f"version {version}" if version is not None else "current definition"
        raise ValueError(f"index '{index_id}' has no {what}")
    return row


async def _evaluations_by_market(
    session, ir: IndexDef, index_id: str
) -> dict[int, dict[str, AuditEvaluation]]:
    """Cached evaluation per (market, factor) usable by THIS index version.

    Index-independent (CORR-1.10): reuses the pipeline's cache loader, which
    keys on ``(market, factor_id, prompt_sha256, model_id)`` and deliberately
    NOT on ``index_definition_id``. So a version bump that only changes
    aggregation (same factors / prompts / models) replays on the *previous*
    version's evals instead of returning an empty backtest until every market
    is re-judged — which is what forced the manual eval copy on the senate v10
    bump before this fix.

    The market universe is every market ever judged under ANY version of this
    ``index_id`` (the honest historical membership); within it,
    ``load_cached_evaluations`` returns exactly the eval whose
    ``(prompt_sha256, model_id)`` matches what this version's IR resolves each
    factor to — tier-2 escalations preferred, failed stubs skipped, the same
    rules the live aggregator applies today. A factor whose prompt/model DID
    change across the bump simply has no matching cached eval (a correct miss),
    not a stale reuse.
    """
    _prompt_rows, resolved_models = await resolve_factor_models(session, ir)

    # Universe: markets judged under any version of this index_id. Broadening
    # from a single index_definition_id (the old filter) is what lets a fresh
    # version see the prior version's evals.
    version_ids = (
        select(CoreIndexDefinition.id)
        .where(CoreIndexDefinition.index_id == index_id)
        .scalar_subquery()
    )
    market_ids = (
        await session.execute(
            select(AuditEvaluation.market_id)
            .where(AuditEvaluation.index_definition_id.in_(version_ids))
            .distinct()
        )
    ).scalars().all()
    if not market_ids:
        return {}

    evalmap = await load_cached_evaluations(
        session, ir, resolved_models, list(market_ids)
    )
    out: dict[int, dict[str, AuditEvaluation]] = {}
    for (market_id, factor_id), ev in evalmap.items():
        out.setdefault(market_id, {})[factor_id] = ev
    return out


async def score_index_as_of(
    index_id: str, as_of: datetime, version: int | None = None
) -> dict:
    """One read-only replay tick: cached evals + prices-as-of → aggregate.

    ``version`` pins which definition row is replayed (default: current).
    Returns a JSON-safe point dict; never writes ts_index_scores.
    """
    async with session_scope() as session:
        index_def = await _load_def(session, index_id, version)
        ir = IndexDef.model_validate(index_def.definition)
        evals = await _evaluations_by_market(session, ir, index_id)
        market_ids = list(evals.keys())
        if not market_ids:
            return {
                "as_of": as_of.isoformat(),
                "score": None,
                "component_count": 0,
                "markets_priced": 0,
                "reason": "no cached evaluations matching this version's factor models",
            }

        markets = (
            await session.execute(select(CoreMarket).where(CoreMarket.id.in_(market_ids)))
        ).scalars().all()

        # Latest snapshot at-or-before as_of, one row per market (DISTINCT ON).
        price_rows = (
            await session.execute(
                select(
                    TsPriceSnapshot.market_id,
                    TsPriceSnapshot.last_price,
                    TsPriceSnapshot.volume_24h,
                )
                .where(
                    TsPriceSnapshot.market_id.in_(market_ids),
                    TsPriceSnapshot.snapshot_at <= as_of,
                )
                .order_by(TsPriceSnapshot.market_id, TsPriceSnapshot.snapshot_at.desc())
                .distinct(TsPriceSnapshot.market_id)
            )
        ).all()
        price_by_market = {
            mid: float(price) for mid, price, _vol in price_rows if price is not None
        }
        volume_by_market = {
            mid: (float(vol) if vol is not None else None) for mid, _price, vol in price_rows
        }

        rows = [
            MarketEvaluations(
                market=m,
                by_factor=evals[m.id],
                last_price=price_by_market[m.id],
                liquidity=volume_by_market.get(m.id),
            )
            for m in markets
            if m.id in price_by_market  # unpriced-as-of-day markets excluded
        ]
        result = aggregate(rows, ir)
        point = {
            "as_of": as_of.isoformat(),
            "score": result.score,
            "component_count": result.component_count,
            "markets_priced": len(rows),
        }
        bd = _breakdown_metrics(result.breakdown)
        if bd:
            point["breakdown_metrics"] = bd
        return point


def _epoch_ms(iso: str) -> int | None:
    """Epoch millis of an ISO instant, or None — the MLflow metric timestamp."""
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _record_backtest_run(
    index_id: str,
    version: int,
    formula: str,
    yaml_sha256: str,
    index_definition_id: int,
    days: int,
    step_hours: int,
    end_at: datetime,
    points: list[dict],
) -> tuple[str | None, str | None]:
    """Mirror one backtest into MLflow as a single comparable run (SHIP-3.6).

    Attaches to the shared per-index experiment ``pmi.<index_id>`` so pinned
    replays of different versions land side by side. version / formula /
    yaml_sha256 are run tags; the per-point score curve is a stepped metric
    (x = point index, aligned across runs), plus one stepped series per curated
    breakdown field (``_BREAKDOWN_METRIC_KEYS`` — seat indexes' p_r_control /
    stdev_r_seats, etc.) so a distribution-only bump shows a divergence the
    E[seats] score can't. Together that IS the comparison surface — MLflow's own
    multi-run view, no bespoke API / CSV alignment.

    Runs on a worker thread (see caller) and is best-effort: MLflow down →
    ``(None, None)`` and the replay result is untouched.
    """
    experiment_id = mlflow_client.ensure_experiment(index_id)
    if experiment_id is None:
        return None, None

    scored = [p for p in points if p.get("score") is not None]
    scores = [float(p["score"]) for p in scored]
    run_name = f"backtest:{index_id}@v{version}:{end_at.date().isoformat()}:{days}d"
    with mlflow_client.start_run(
        experiment_id=experiment_id,
        run_name=run_name,
        tags={
            "kind": "backtest",
            "index_id": index_id,
            "index_version": str(version),
            "formula": formula,
            "yaml_sha256": yaml_sha256,
            "index_definition_id": str(index_definition_id),
        },
    ) as run_id:
        mlflow_client.log_params(
            run_id,
            {
                "index_id": index_id,
                "index_version": version,
                "formula": formula,
                "yaml_sha256": yaml_sha256,
                "days": days,
                "step_hours": step_hours,
                "end": end_at.isoformat(),
                "n_points": len(points),
            },
        )
        mlflow_client.log_metric_series(
            run_id,
            "score",
            ((p.get("score"), i, _epoch_ms(p["as_of"])) for i, p in enumerate(points)),
        )
        mlflow_client.log_metric_series(
            run_id,
            "markets_priced",
            (
                (p.get("markets_priced"), i, _epoch_ms(p["as_of"]))
                for i, p in enumerate(points)
            ),
        )

        # Distribution / quality series from each point's breakdown — the
        # ρ-sensitive seat numbers (p_r_control, stdev_r_seats, …) a
        # seat_projection version bump moves even when the E[seats] score does
        # not. Generic: senate / house / governor all surface these; non-seat
        # indexes surface `raw`. Union of keys across points (an early-window
        # point that didn't score contributes none) → one series each.
        bd_keys = sorted({k for p in points for k in (p.get("breakdown_metrics") or {})})
        bd_summary: dict[str, float | None] = {}
        for key in bd_keys:
            mlflow_client.log_metric_series(
                run_id,
                key,
                (
                    ((p.get("breakdown_metrics") or {}).get(key), i, _epoch_ms(p["as_of"]))
                    for i, p in enumerate(points)
                ),
            )
            vals = [
                (p.get("breakdown_metrics") or {}).get(key)
                for p in points
                if (p.get("breakdown_metrics") or {}).get(key) is not None
            ]
            if vals:
                bd_summary[f"{key}_last"] = vals[-1]

        mlflow_client.log_metrics(
            run_id,
            {
                "n_points": len(points),
                "n_scored": len(scored),
                "coverage": (len(scored) / len(points)) if points else 0.0,
                "score_mean": (sum(scores) / len(scores)) if scores else None,
                "score_first": scores[0] if scores else None,
                "score_last": scores[-1] if scores else None,
                "score_min": min(scores) if scores else None,
                "score_max": max(scores) if scores else None,
                **bd_summary,
            },
        )
    return run_id, experiment_id


@workflow("backtest")
async def backtest(
    ctx: WorkflowContext,
    index_id: str,
    days: int = 90,
    step_hours: int = 24,
    end: str | None = None,
    version: int | None = None,
) -> dict:
    """Replay `index_id` over the trailing window, one durable step per point.

    ``version`` pins the definition replayed (default: current). Each run is
    mirrored to MLflow as one comparable run, so pinning different versions over
    the same window diffs their formulas/weights in the MLflow UI (SHIP-3.6).
    """
    end_at = datetime.fromisoformat(end) if end else datetime.now(UTC)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)

    # Resolve the pinned/current definition once — fail fast on a bad version,
    # and capture the concrete version so every replay step (and the MLflow
    # tags) reference the same row even if `is_current` moves under a resume.
    async with session_scope() as session:
        def_row = await _load_def(session, index_id, version)
        resolved_version = def_row.version
        formula = IndexDef.model_validate(def_row.definition).aggregation.formula
        yaml_sha256 = def_row.yaml_sha256
        index_definition_id = def_row.id

    n_points = max(1, int(days * 24 / step_hours))
    points_at = [
        end_at - timedelta(hours=step_hours * i) for i in range(n_points - 1, -1, -1)
    ]
    await ctx.set_steps_total(len(points_at))

    points: list[dict] = []
    for at in points_at:
        key = f"point:{at.isoformat(timespec='seconds')}"
        points.append(
            await ctx.step(key, score_index_as_of, index_id, at, resolved_version)
        )

    scored = [p for p in points if p.get("score") is not None]

    # Mirror to MLflow off-thread so a slow tracking server never blocks the
    # worker loop. Best-effort — NULL ids when MLflow is unreachable.
    mlflow_run_id, mlflow_experiment_id = await asyncio.to_thread(
        _record_backtest_run,
        index_id,
        resolved_version,
        formula,
        yaml_sha256,
        index_definition_id,
        days,
        step_hours,
        end_at,
        points,
    )

    log.info(
        "backtest.done",
        index_id=index_id,
        version=resolved_version,
        points=len(points),
        scored=len(scored),
        mlflow_run_id=mlflow_run_id,
    )
    return {
        "index_id": index_id,
        "version": resolved_version,
        "days": days,
        "step_hours": step_hours,
        "end": end_at.isoformat(),
        "points": points,
        "n_points": len(points),
        "n_scored": len(scored),
        "mlflow_run_id": mlflow_run_id,
        "mlflow_experiment_id": mlflow_experiment_id,
    }
