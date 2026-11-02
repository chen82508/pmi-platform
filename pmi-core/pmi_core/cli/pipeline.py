"""`pmi-core` CLI.

Flat command surface matches the workspace `justfile` contract:

    pmi-core migrate        # alembic upgrade head
    pmi-core seed           # load /app/fixtures/markets.json (if present) + register index def
    pmi-core score <ID>     # one pipeline tick
    pmi-core history <ID>   # print N most recent ts_index_scores rows
    pmi-core list-defs      # list index_defs YAML files on disk
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.config import settings
from pmi_core.db import session_scope

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(os.environ.get("PMI_FIXTURES_DIR", "/app/fixtures"))


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


@click.group()
def cli() -> None:
    """pmi-core CLI."""
    _setup_logging()


@cli.command()
@click.argument("revision", default="head")
def migrate(revision: str) -> None:
    """Apply alembic migrations up to REVISION (default: head)."""
    cmd = ["alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", revision]
    click.echo(f"$ {' '.join(cmd)}")
    sys.exit(subprocess.call(cmd, cwd=REPO_ROOT))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


async def _seed_markets_from_fixture(session: AsyncSession, fixture_path: Path) -> tuple[int, int]:
    """UPSERT core_markets + write ts_price_snapshots per market with prices.

    Optional fixture fields: ``closes_in_days`` sets ``closes_at`` relative to
    now (so demo data never expires), and ``price_history`` is a list of daily
    prices for the days before ``last_price``, written oldest first.

    Returns (markets_touched, price_snapshots_written).
    """
    from pmi_core.models import CoreMarket, TsPriceSnapshot

    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise click.ClickException(f"{fixture_path}: expected JSON list of markets")

    markets_touched = 0
    snapshots_written = 0
    now = datetime.now(UTC)

    for m in data:
        venue = m.get("venue") or "polymarket"
        external_id = str(m.get("external_id") or m.get("id"))
        if not external_id or external_id == "None":
            continue
        closes_at = _parse_dt(m.get("closes_at"))
        if m.get("closes_in_days") is not None:
            closes_at = now + timedelta(days=float(m["closes_in_days"]))
        stmt = pg_insert(CoreMarket).values(
            venue=venue,
            external_id=external_id,
            slug=m.get("slug"),
            title=m.get("title") or m.get("question") or "(untitled fixture)",
            description=m.get("description"),
            category=m.get("category"),
            tags=m.get("tags"),
            opens_at=_parse_dt(m.get("opens_at")),
            closes_at=closes_at,
            resolved_at=_parse_dt(m.get("resolved_at")),
            resolution=m.get("resolution"),
            raw=m,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["venue", "external_id"],
            set_={
                "title": stmt.excluded.title,
                "description": stmt.excluded.description,
                "category": stmt.excluded.category,
                "tags": stmt.excluded.tags,
                "closes_at": stmt.excluded.closes_at,
                "raw": stmt.excluded.raw,
                "updated_at": now,
            },
        ).returning(CoreMarket.id)
        market_id = (await session.execute(stmt)).scalar_one()
        markets_touched += 1

        last_price = m.get("last_price")
        history = m.get("price_history") or []
        for days_back, price in zip(range(len(history), 0, -1), history, strict=True):
            session.add(
                TsPriceSnapshot(
                    market_id=market_id,
                    snapshot_at=now - timedelta(days=days_back),
                    last_price=price,
                    volume_24h=m.get("volume_24h"),
                )
            )
            snapshots_written += 1
        if last_price is not None:
            session.add(
                TsPriceSnapshot(
                    market_id=market_id,
                    snapshot_at=now,
                    last_price=last_price,
                    volume_24h=m.get("volume_24h"),
                )
            )
            snapshots_written += 1

    return markets_touched, snapshots_written


async def _seed_async() -> None:
    """Best-effort: load fixture markets if present, then ensure the baseline IndexDef row."""
    from pmi_core.dsl.ir import load_index_def
    from pmi_core.engine.pipeline import ensure_index_definition

    fixture = FIXTURES_DIR / "markets.json"

    async with session_scope() as session:
        if fixture.exists():
            markets, snapshots = await _seed_markets_from_fixture(session, fixture)
            click.echo(
                f"✓ seeded {markets} markets, {snapshots} price snapshots from {fixture}"
            )
        else:
            click.echo(f"… no fixture at {fixture}; skipping market seed (ingest will populate).")

        index_dir = REPO_ROOT / "pmi_core" / "index_defs"
        for yaml_path in sorted(index_dir.glob("*.yaml")):
            ir, yaml_text, sha256 = load_index_def(yaml_path)
            row = await ensure_index_definition(session, ir, yaml_text, sha256)
            click.echo(f"✓ index_def registered: {row.index_id} v{row.version} (id={row.id})")


@cli.command()
def seed() -> None:
    """Load fixture markets + register baseline index definitions."""
    asyncio.run(_seed_async())


@cli.command()
@click.argument("index_id", default="war-index-2026")
def score(index_id: str) -> None:
    """Run one pipeline tick for INDEX_ID and persist to ts_index_scores."""
    from pmi_core.engine import run_pipeline

    result = asyncio.run(run_pipeline(index_id=index_id))
    click.echo(json.dumps(result, indent=2, default=str))


@cli.command()
@click.argument("index_id")
@click.option("--days", default=14, show_default=True, help="Re-score points within the last N days.")
@click.option("--dry-run", is_flag=True, help="Show old->new without writing.")
def backfill(index_id: str, days: int, dry_run: bool) -> None:
    """Re-score existing ts_index_scores points for INDEX_ID over the last --days.

    Rebuilds each stored point from its historical prices + the current factor
    evals and reruns the aggregator (no LLM re-eval). Generic across verticals;
    use after a scoring-logic change to correct stale historical points.
    """
    from pmi_core.engine.pipeline import backfill_scores

    since = datetime.now(UTC) - timedelta(days=days)
    result = asyncio.run(backfill_scores(index_id=index_id, since=since, dry_run=dry_run))
    if result.get("error"):
        click.echo(f"error: {result['error']}")
        return
    moved = 0
    for c in result["changes"]:
        old = "None" if c["old"] is None else f"{c['old']:.1f}"
        new = "None" if c["new"] is None else f"{c['new']:.1f}"
        delta = (
            abs(c["old"] - c["new"])
            if c["old"] is not None and c["new"] is not None
            else 0.0
        )
        if delta >= 0.05:
            moved += 1
        flag = " *" if delta >= 0.05 else ""
        click.echo(f"  {c['as_of']}  {old:>6} -> {new:>6}{flag}")
    verb = "would update" if dry_run else "updated"
    click.echo(f"{result['index_id']}: {result['points']} points, {verb} {moved} changed.")




async def _history_async(index_id: str, limit: int) -> list[dict]:
    from pmi_core.models import CoreIndexDefinition, TsIndexScore

    async with session_scope() as session:
        def_row = (
            await session.execute(
                select(CoreIndexDefinition)
                .where(CoreIndexDefinition.index_id == index_id)
                .order_by(CoreIndexDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if def_row is None:
            return []
        rows = (
            await session.execute(
                select(TsIndexScore)
                .where(TsIndexScore.index_definition_id == def_row.id)
                .order_by(TsIndexScore.as_of.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "as_of": r.as_of.isoformat(),
                "score": float(r.score),
                "component_count": r.component_count,
                "breakdown": r.breakdown,
            }
            for r in rows
        ]


@cli.command()
@click.argument("index_id", default="war-index-2026")
@click.option("--limit", default=10, type=int)
def history(index_id: str, limit: int) -> None:
    """Print the most recent N ts_index_scores rows for INDEX_ID."""
    rows = asyncio.run(_history_async(index_id, limit))
    click.echo(json.dumps(rows, indent=2))


async def _calibrate_correlation_async(
    index_id: str, target: float, party: str
) -> dict:
    """Fit the national-swing ρ for a seat-projection index against a target.

    Reuses the latest persisted score's component markets (no re-evaluation):
    pulls one P(R wins) per contested seat exactly as the /senate-board
    endpoint does, then bisects ρ so model P(party control) == target.
    """
    from pmi_core.engine.seat_distribution import (
        calibrate_correlation,
        compute_seat_distribution,
    )
    from pmi_core.engine.seat_mapping import extract_contested_seats
    from pmi_core.models import (
        AuditEvaluation,
        CoreIndexDefinition,
        CoreMarket,
        TsIndexScore,
        TsPriceSnapshot,
    )

    async with session_scope() as session:
        def_row = (
            await session.execute(
                select(CoreIndexDefinition)
                .where(CoreIndexDefinition.index_id == index_id)
                .order_by(CoreIndexDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if def_row is None:
            raise click.ClickException(f"no index definition for {index_id!r}")

        sp = ((def_row.definition or {}).get("aggregation") or {}).get(
            "seat_projection"
        ) or {}

        score = (
            await session.execute(
                select(TsIndexScore)
                .where(TsIndexScore.index_definition_id == def_row.id)
                .order_by(TsIndexScore.as_of.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if score is None or not score.component_evaluation_ids:
            raise click.ClickException(
                f"no scored components for {index_id!r}; run `score` first"
            )

        market_ids = list(
            (
                await session.execute(
                    select(AuditEvaluation.market_id)
                    .where(AuditEvaluation.id.in_(score.component_evaluation_ids))
                    .distinct()
                )
            ).scalars().all()
        )
        titles = {
            m.id: m.title
            for m in (
                await session.execute(
                    select(CoreMarket).where(CoreMarket.id.in_(market_ids))
                )
            ).scalars().all()
        }
        prices: dict[int, float] = {}
        for mid, price in (
            await session.execute(
                select(TsPriceSnapshot.market_id, TsPriceSnapshot.last_price)
                .where(
                    TsPriceSnapshot.market_id.in_(market_ids),
                    TsPriceSnapshot.snapshot_at <= score.as_of,
                )
                .order_by(
                    TsPriceSnapshot.market_id, TsPriceSnapshot.snapshot_at.desc()
                )
                .distinct(TsPriceSnapshot.market_id)
            )
        ).all():
            if price is not None:
                prices[mid] = float(price)

        seats = extract_contested_seats(
            [(mid, titles.get(mid, ""), prices[mid]) for mid in market_ids if mid in prices]
        )
        probs = [s.prob_r for s in seats]

    geometry = {
        "holdover_r": int(sp.get("holdover_r", 0)),
        "holdover_d": int(sp.get("holdover_d", 0)),
        "total_seats": int(sp.get("total_seats", 100)),
        "majority_threshold": int(sp.get("majority_threshold", 51)),
        "tie_break": sp.get("tie_break"),
    }
    before = compute_seat_distribution(probs, correlation=None, **geometry)
    rho = calibrate_correlation(probs, target, party=party, **geometry)
    after = (
        compute_seat_distribution(probs, correlation=rho, **geometry)
        if rho is not None
        else None
    )

    def view(d) -> dict:
        return {
            "p_r_control": round(d.p_r_control * 100, 2),
            "p_d_control": round(d.p_d_control * 100, 2),
            "p_r_majority": round(d.p_r_majority * 100, 2),
            "expected_r_seats": round(d.expected_r_seats, 2),
            "stdev_r_seats": round(d.stdev_r_seats, 2),
        }

    return {
        "index_id": index_id,
        "version": def_row.version,
        "n_contested": len(probs),
        "target_control": {"party": party, "value": round(target * 100, 2)},
        "fitted_correlation": None if rho is None else round(rho, 4),
        "unreachable": rho is None,
        "independent": view(before),
        "calibrated": None if after is None else view(after),
    }


@cli.command("calibrate-correlation")
@click.argument("index_id")
@click.option(
    "--target",
    required=True,
    type=float,
    help="Target P(party control), 0..1 — e.g. the de-vigged direct control market.",
)
@click.option(
    "--party", default="R", type=click.Choice(["R", "D"]), help="Which party's control."
)
def calibrate_correlation_cmd(index_id: str, target: float, party: str) -> None:
    """Fit the national-swing ρ for a seat-projection INDEX_ID.

    Prints the ρ whose seat distribution reproduces --target P(party control);
    pin it into the index YAML's `aggregation.seat_projection.correlation`
    (bump the version) so scoring stays deterministic.
    """
    if not 0.0 < target < 1.0:
        raise click.ClickException("--target must be in (0, 1)")
    result = asyncio.run(_calibrate_correlation_async(index_id, target, party))
    click.echo(json.dumps(result, indent=2))


@cli.command()
@click.option("--venue", default=None, help="Restrict to one venue (e.g. polymarket).")
@click.option(
    "--lead-hours",
    default=24,
    type=int,
    help="Score the last snapshot at least this many hours before resolution.",
)
def brier(venue: str | None, lead_hours: int) -> None:
    """Brier calibration of market prices vs resolved YES/NO outcomes (T6).

    Needs ground truth from the resolution sweeper
    (`pmi-ingest run --source polymarket-resolutions`).
    """
    from pmi_core.engine.brier import brier_report

    report = asyncio.run(brier_report(venue=venue, lead_hours=lead_hours))
    click.echo(json.dumps(report, indent=2))


@cli.command("list-defs")
def list_defs() -> None:
    """List index_defs available on disk."""
    defs = sorted((REPO_ROOT / "pmi_core" / "index_defs").glob("*.yaml"))
    for d in defs:
        click.echo(d.stem)


# ──────────────────────────────────────────────────────────────────────────
# dry-run: pipeline in-process, never touches the DB
# ──────────────────────────────────────────────────────────────────────────


@cli.command("dry-run")
@click.argument(
    "yaml_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--fixture",
    "fixture_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JSON list of markets. Default: pmi_data_platform/pmi-demo/fixtures/markets.json",
)
@click.option(
    "--compact/--full",
    default=False,
    help="Compact prints only score + counts. Full prints per-factor evaluations.",
)
def dry_run(yaml_path: Path, fixture_path: Path | None, compact: bool) -> None:
    """Run the pipeline IN MEMORY against a fixture — never writes to the DB.

    Useful for iterating on a YAML index_def: tweak a selector or factor
    weight, re-run, diff the report. No Postgres, no MLflow, no migrations.
    """
    from pmi_core.dsl.ir import load_index_def
    from pmi_core.engine.dry_run import dry_run as engine_dry_run

    if fixture_path is None:
        candidate = REPO_ROOT.parent / "pmi-demo" / "fixtures" / "markets.json"
        if not candidate.exists():
            raise click.ClickException(
                f"No fixture at {candidate}. Pass --fixture explicitly."
            )
        fixture_path = candidate

    ir, _yaml_text, sha256 = load_index_def(yaml_path)
    report = engine_dry_run(ir, fixture_path, yaml_sha256=sha256)

    if compact:
        click.echo(
            json.dumps(
                {
                    "index_id": report.index_id,
                    "version": report.version,
                    "score": report.aggregation["score"],
                    "component_count": report.aggregation["component_count"],
                    "selectors_candidates": report.selectors["candidates_returned"],
                    "fixture_market_count": report.fixture_market_count,
                    "formula_declared": report.aggregation["formula_declared"],
                    "formula_used": report.aggregation["formula_used"],
                },
                indent=2,
            )
        )
    else:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))


# ──────────────────────────────────────────────────────────────────────────
# schema: dump JSON Schema for IndexDef YAML
# ──────────────────────────────────────────────────────────────────────────


SCHEMA_OUTPUT_PATH = REPO_ROOT / "pmi_core" / "dsl" / "schema" / "index-def.schema.json"


@cli.group("schema")
def schema() -> None:
    """JSON Schema utilities for the IndexDef YAML DSL."""


@schema.command("dump")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Where to write the schema. Default: {SCHEMA_OUTPUT_PATH}",
)
@click.option(
    "--stdout",
    "to_stdout",
    is_flag=True,
    default=False,
    help="Print to stdout instead of writing to a file.",
)
def schema_dump(output_path: Path | None, to_stdout: bool) -> None:
    """Emit JSON Schema (draft 2020-12) for IndexDef. Run after changing IR fields."""
    from pmi_core.dsl.ir import IndexDef

    schema_obj = IndexDef.model_json_schema()
    schema_obj["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema_obj["$id"] = "https://pmi-platform.example.com/schema/index-def.schema.json"
    text = json.dumps(schema_obj, indent=2, sort_keys=True) + "\n"

    if to_stdout:
        click.echo(text, nl=False)
        return

    dest = output_path or SCHEMA_OUTPUT_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    click.echo(f"✓ wrote {dest} ({len(text)} bytes)")


# ──────────────────────────────────────────────────────────────────────────
# MLflow
# ──────────────────────────────────────────────────────────────────────────


async def _mlflow_init_async() -> dict:
    """Backfill MLflow links for any rows missing them.

    1. Ensure an MLflow experiment exists for every index_id and persist its id
       on `core_index_definitions.mlflow_experiment_id` (where NULL).
    2. Register every `core_prompts` row to MLflow Prompt Registry and persist
       its URI on `core_prompts.mlflow_prompt_uri` (where NULL).

    Safe to re-run: skips rows already populated, idempotent on MLflow side via
    sha256 tag.
    """
    from pmi_core import mlflow_client
    from pmi_core.models import CoreIndexDefinition, CorePrompt

    if not mlflow_client.is_enabled():
        raise click.ClickException(
            f"MLflow at {settings.mlflow_tracking_uri} unreachable. "
            "Bring up `just mlflow-up` first or set PMI_MLFLOW_ENABLED=false."
        )

    experiments_linked = 0
    prompts_linked = 0

    async with session_scope() as session:
        for row in (
            await session.execute(
                select(CoreIndexDefinition).where(
                    CoreIndexDefinition.mlflow_experiment_id.is_(None)
                )
            )
        ).scalars().all():
            exp_id = mlflow_client.ensure_experiment(row.index_id)
            if exp_id is not None:
                row.mlflow_experiment_id = exp_id
                experiments_linked += 1

        for row in (
            await session.execute(
                select(CorePrompt).where(CorePrompt.mlflow_prompt_uri.is_(None))
            )
        ).scalars().all():
            uri = mlflow_client.register_prompt(
                name=row.name,
                template=row.template,
                sha256=row.sha256,
                tags={"version": str(row.version), "source": "backfill"},
            )
            if uri:
                row.mlflow_prompt_uri = uri
                prompts_linked += 1

    return {
        "tracking_uri": settings.mlflow_tracking_uri,
        "experiments_linked": experiments_linked,
        "prompts_linked": prompts_linked,
    }


@cli.command("mlflow-init")
def mlflow_init() -> None:
    """Backfill MLflow experiments + prompt URIs onto existing rows. Idempotent."""
    result = asyncio.run(_mlflow_init_async())
    click.echo(json.dumps(result, indent=2))


async def _prompts_list_async() -> list[dict]:
    from pmi_core.models import CorePrompt

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CorePrompt).order_by(CorePrompt.name, CorePrompt.version)
            )
        ).scalars().all()
        return [
            {
                "name": r.name,
                "version": r.version,
                "sha256": r.sha256[:12],
                "mlflow_prompt_uri": r.mlflow_prompt_uri,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


async def _prompts_ensure_async(ref: str) -> dict:
    from pmi_core.engine.pipeline import ensure_prompt

    async with session_scope() as session:
        row = await ensure_prompt(session, ref)
        await session.commit()
        return {
            "id": row.id,
            "name": row.name,
            "version": row.version,
            "sha256": row.sha256,
            "mlflow_prompt_uri": row.mlflow_prompt_uri,
        }


@cli.command("prompts")
@click.argument("action", type=click.Choice(["list", "ensure"], case_sensitive=False))
@click.argument("ref", required=False)
def prompts(action: str, ref: str | None) -> None:
    """`prompts list` — show every CorePrompt row with its MLflow URI.

    `prompts ensure prompts/factors/<name>-vN` — register a git prompt file
    into core_prompts (append-only; sha mismatch on an existing version
    raises). Use this to stage a new prompt version for `models register`
    without bumping any index definition.
    """
    if action.lower() == "list":
        rows = asyncio.run(_prompts_list_async())
        click.echo(json.dumps(rows, indent=2))
        return
    if not ref:
        raise click.ClickException("usage: prompts ensure prompts/factors/<name>-vN")
    result = asyncio.run(_prompts_ensure_async(ref))
    click.echo(json.dumps(result, indent=2))


# ──────────────────────────────────────────────────────────────────────────
# Factor Models — Model Registry mirror
# ──────────────────────────────────────────────────────────────────────────


async def _models_list_async(factor: str | None) -> list[dict]:
    from pmi_core.models import CoreFactorModel, CorePrompt

    async with session_scope() as session:
        stmt = (
            select(CoreFactorModel, CorePrompt)
            .join(CorePrompt, CorePrompt.id == CoreFactorModel.prompt_id)
            .order_by(CoreFactorModel.factor_id, CoreFactorModel.version)
        )
        if factor:
            stmt = stmt.where(CoreFactorModel.factor_id == factor)
        rows = (await session.execute(stmt)).all()
        return [
            {
                "id": fm.id,
                "factor_id": fm.factor_id,
                "version": fm.version,
                "prompt": f"{cp.name}-v{cp.version}",
                "prompt_sha256_short": cp.sha256[:12],
                "llm_model_id": fm.llm_model_id,
                "temperature": float(fm.temperature) if fm.temperature is not None else None,
                "stage": fm.stage,
                "is_active": fm.is_active,
                "mlflow_registered_model_name": fm.mlflow_registered_model_name,
                "mlflow_model_version": fm.mlflow_model_version,
                "description": fm.description,
                "created_at": fm.created_at.isoformat() if fm.created_at else None,
                "created_by": fm.created_by,
            }
            for fm, cp in rows
        ]


async def _models_doctor_async() -> dict:
    """Cross-check every live index def's factors against the registry.

    Two failure modes, both silent by construction:

    ``unbound``  a factor in a current def with no active production row.
                 ``resolve_factor_model`` falls back to the deterministic stub
                 exactly as designed, so the index keeps publishing — with
                 membership decided by a hash. This is what happened to the S&P
                 for eight days.
    ``orphaned`` a registry row whose factor_id no current def mentions. Alone
                 it is harmless; paired with an unbound factor of a similar name
                 it is the signature of a rename that only got applied on one
                 side, which is the diagnosis rather than the symptom.
    """
    from pmi_core.dsl.ir import load_index_def
    from pmi_core.engine.factor_resolver import DEFAULT_STUB_MODEL_ID
    from pmi_core.models import CoreFactorModel

    # On-disk defs, not the DB rows: this is what `run_pipeline` will read on the
    # next tick, so it is what the registry has to line up with. The non-
    # recursive glob skips index_defs/deprecated/ for the same reason seed does.
    index_dir = REPO_ROOT / "pmi_core" / "index_defs"
    factors_by_index: dict[str, list[str]] = {}
    for yaml_path in sorted(index_dir.glob("*.yaml")):
        ir, _raw, _sha = load_index_def(yaml_path)
        factors_by_index[ir.id] = [f.id for f in ir.factors]
    declared = {fid for fids in factors_by_index.values() for fid in fids}

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CoreFactorModel).where(
                    CoreFactorModel.stage == "production",
                    CoreFactorModel.is_active.is_(True),
                )
            )
        ).scalars().all()
    bound = {r.factor_id: r.llm_model_id for r in rows}

    unbound = [
        {"index_id": iid, "factor_id": fid, "resolves_to": DEFAULT_STUB_MODEL_ID}
        for iid, fids in sorted(factors_by_index.items())
        for fid in fids
        if fid not in bound
    ]
    orphaned = [
        {"factor_id": fid, "llm_model_id": model}
        for fid, model in sorted(bound.items())
        if fid not in declared
    ]
    stubbed_indexes = sorted({u["index_id"] for u in unbound})
    return {
        "index_defs": len(factors_by_index),
        "declared_factors": len(declared),
        "production_rows": len(bound),
        "unbound": unbound,
        "orphaned": orphaned,
        "indexes_with_stubbed_factors": stubbed_indexes,
        "ok": not unbound and not orphaned,
    }


async def _models_register_async(
    factor_id: str,
    prompt_name: str,
    prompt_version: int,
    llm_model_id: str,
    temperature: float | None,
    description: str | None,
    created_by: str | None,
    mlflow_register: bool,
    tools_config: dict | None = None,
) -> dict:
    from pmi_core import mlflow_client
    from pmi_core.models import CoreFactorModel, CorePrompt

    async with session_scope() as session:
        prompt_row = (
            await session.execute(
                select(CorePrompt).where(
                    CorePrompt.name == prompt_name,
                    CorePrompt.version == prompt_version,
                )
            )
        ).scalar_one_or_none()
        if prompt_row is None:
            raise click.ClickException(
                f"Prompt {prompt_name}-v{prompt_version} not found in core_prompts. "
                "Run `pmi-core score` once to register baseline prompts, or check `prompts list`."
            )

        next_version = (
            (
                await session.execute(
                    select(CoreFactorModel.version)
                    .where(CoreFactorModel.factor_id == factor_id)
                    .order_by(CoreFactorModel.version.desc())
                    .limit(1)
                )
            ).scalar()
            or 0
        ) + 1

        mlflow_registered_model_name = None
        mlflow_model_version = None
        if mlflow_register and mlflow_client.is_enabled():
            mlflow_registered_model_name = f"pmi.factor.{factor_id}"
            # Real model artifact registration lands when LLM is real (P1).
            # For now we just reserve the name; version stays NULL until the
            # first real artifact lands.
            mlflow_model_version = None

        fm = CoreFactorModel(
            factor_id=factor_id,
            version=next_version,
            prompt_id=prompt_row.id,
            llm_model_id=llm_model_id,
            temperature=temperature,
            tools_config=tools_config,
            stage="staging",
            is_active=False,
            description=description,
            created_by=created_by,
            mlflow_registered_model_name=mlflow_registered_model_name,
            mlflow_model_version=mlflow_model_version,
        )
        session.add(fm)
        await session.flush()

        return {
            "id": fm.id,
            "factor_id": fm.factor_id,
            "version": fm.version,
            "prompt": f"{prompt_row.name}-v{prompt_row.version}",
            "llm_model_id": fm.llm_model_id,
            "temperature": float(fm.temperature) if fm.temperature is not None else None,
            "stage": fm.stage,
            "is_active": fm.is_active,
            "mlflow_registered_model_name": fm.mlflow_registered_model_name,
        }


async def _models_promote_async(model_id: int, stage: str) -> dict:
    from pmi_core.models import CoreFactorModel

    async with session_scope() as session:
        target = (
            await session.execute(select(CoreFactorModel).where(CoreFactorModel.id == model_id))
        ).scalar_one_or_none()
        if target is None:
            raise click.ClickException(f"No CoreFactorModel with id={model_id}")

        # Demote any current active row at the same (factor_id, stage).
        demoted = []
        prev = (
            await session.execute(
                select(CoreFactorModel).where(
                    CoreFactorModel.factor_id == target.factor_id,
                    CoreFactorModel.stage == stage,
                    CoreFactorModel.is_active.is_(True),
                    CoreFactorModel.id != target.id,
                )
            )
        ).scalars().all()
        for row in prev:
            row.is_active = False
            demoted.append({"id": row.id, "version": row.version})

        target.stage = stage
        target.is_active = True
        await session.flush()

        return {
            "promoted": {
                "id": target.id,
                "factor_id": target.factor_id,
                "version": target.version,
                "stage": target.stage,
                "is_active": target.is_active,
            },
            "demoted": demoted,
        }


# ──────────────────────────────────────────────────────────────────────────
# eval: gold-set harness for classification / membership changes
# ──────────────────────────────────────────────────────────────────────────


@cli.group("eval")
def eval_group() -> None:
    """Measure membership classification against a pinned gold set.

    Order of operations:

        pmi-core eval sample btc-membership     # stratified draw (read-only)
        pmi-core eval label  btc-membership     # model-only labelling ($)
        pmi-core eval score  btc-membership     # baseline: live vs structural

    `sample` and `score` are free; only `label` spends. All three write to
    pmi_core/eval/gold/, which is version-controlled on purpose — the gold set
    is a definition, not a cache.
    """


@eval_group.command("sample")
@click.argument("taxonomy")
@click.option("--seed", default=20260802, type=int, help="Reproducible draw.")
@click.option(
    "--size",
    "sizes",
    multiple=True,
    help="Override one stratum, e.g. --size near_miss=100. Repeatable.",
)
@click.option("--note", default="", help="Recorded in the sidecar for future readers.")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing gold file (discards its labels — normally you bump the version instead).",
)
def eval_sample(taxonomy: str, seed: int, sizes: tuple[str, ...], note: str, force: bool) -> None:
    """Draw a stratified sample into an UNLABELLED gold file."""
    from pmi_core.eval.sampling import sample_gold
    from pmi_core.eval.schema import gold_paths, resolve_spec, save_gold

    spec = resolve_spec(taxonomy)
    jsonl_path, _meta_path = gold_paths(spec)
    if jsonl_path.exists() and not force:
        raise click.ClickException(
            f"{jsonl_path} already exists. Bump GoldSpec.version for a new set, or --force to discard."
        )

    per_stratum: dict[str, int] = {}
    for raw in sizes:
        key, _, val = raw.partition("=")
        if not val.isdigit():
            raise click.ClickException(f"--size expects name=INT, got {raw!r}")
        per_stratum[key.strip()] = int(val)

    meta, items = asyncio.run(sample_gold(spec, per_stratum, seed=seed, note=note))
    paths = save_gold(spec, meta, items, None)
    click.echo(
        json.dumps(
            {
                "taxonomy": spec.taxonomy,
                "items": len(items),
                "strata": {
                    k: {"sampled": v.sampled, "population": v.population}
                    for k, v in meta.strata.items()
                },
                "files": [str(p) for p in paths],
            },
            indent=2,
        )
    )


@eval_group.command("label")
@click.argument("taxonomy")
@click.option(
    "--labeller",
    "labellers",
    multiple=True,
    default=("gpt-4o-mini-2024-07-18",),
    help="Independent labeller model. Pass twice for two — one alone cannot reach consensus.",
)
@click.option("--samples", default=3, type=int, help="Self-consistency samples per labeller.")
@click.option("--temperature", default=0.3, type=float)
@click.option("--relabel", is_flag=True, default=False, help="Re-label already-labelled items.")
@click.option("--limit", default=0, type=int, help="Label at most N items (smoke test).")
@click.option(
    "--question",
    "questions",
    multiple=True,
    help="Label only these factors (repeatable). Default: every question in the spec. "
    "Each question is its own LLM call, so the membership question alone is ~3x cheaper.",
)
def eval_label(
    taxonomy: str,
    labellers: tuple[str, ...],
    samples: int,
    temperature: float,
    relabel: bool,
    limit: int,
    questions: tuple[str, ...],
) -> None:
    """Run the model-only labelling protocol over a sampled gold file."""
    from pmi_core.eval.labeling import LabellerConfig, label_items
    from pmi_core.eval.schema import load_gold, resolve_spec, save_gold

    spec = resolve_spec(taxonomy)
    meta, items = load_gold(spec)

    production = asyncio.run(_eval_production_models(spec.membership_question))
    overlap = sorted(set(labellers) & production)
    if overlap:
        click.echo(
            f"WARNING: {overlap} already produced production judgments for "
            f"'{spec.membership_question}'. A labeller that is the system under test "
            "grades its own homework — prefer a model not bound to this factor.",
            err=True,
        )
    if len(labellers) < 2:
        click.echo(
            "WARNING: one labeller + one structural vote is the minimum for consensus, "
            "and questions with no structural voter will ALL come out contested. "
            "Pass --labeller twice for a real protocol.",
            err=True,
        )

    subset = items[:limit] if limit else items
    cfg = LabellerConfig(
        models=tuple(labellers), samples_per_model=samples, temperature=temperature
    )
    _labelled, stats = asyncio.run(
        label_items(spec, subset, cfg, relabel=relabel, questions=questions or None)
    )
    save_gold(spec, meta, items, None)
    click.echo(json.dumps({"taxonomy": spec.taxonomy, **stats.as_dict()}, indent=2))


async def _eval_production_models(factor_id: str) -> set[str]:
    from pmi_core.models import AuditEvaluation

    async with session_scope() as session:
        rows = await session.execute(
            select(AuditEvaluation.model_id)
            .where(AuditEvaluation.factor_id == factor_id)
            .distinct()
        )
        return {r[0] for r in rows.all()}


@eval_group.command("score")
@click.argument("taxonomy")
@click.option(
    "--system",
    "systems",
    multiple=True,
    default=("live", "structural"),
    help="Which systems to score: 'live', 'structural', or 'labeller:<model id>' to "
    "replay a labeller's own stored votes as if that model were in production. "
    "Default scores live + structural and diffs them.",
)
@click.option("--errors/--no-errors", default=True, help="Include misclassified examples.")
@click.option(
    "--exclude-labeller",
    "exclude_labellers",
    multiple=True,
    help="Re-derive gold from stored votes with these labellers dropped. Use this when a "
    "system under test also voted on the labels (e.g. the production model, or "
    "'structural') — otherwise it is grading its own homework. Free: no LLM calls.",
)
def eval_score(
    taxonomy: str,
    systems: tuple[str, ...],
    errors: bool,
    exclude_labellers: tuple[str, ...],
) -> None:
    """Score systems against the gold set and print the comparison.

    'live' is whatever the taxonomy says the running system is — selector →
    Tier 0 → stored factor judgment for an index def, the regex chain for the
    GDP board. 'structural' is the $0 deterministic rule. The gap between them
    is the honest measure of what the LLM is buying on this taxonomy — but only
    if neither helped write the gold, which is what --exclude-labeller is for.

    'labeller:<model id>' answers a different question: which model should this
    factor be bound to. Labelling already asked every candidate the membership
    question on every gold item, so scoring one is a lookup over stored votes
    rather than a run — pair it with --exclude-labeller <same model> so the
    candidate is not graded against a consensus it wrote.
    """
    from pmi_core.eval.labeling import readjudicate
    from pmi_core.eval.metrics import compare, evaluate_membership
    from pmi_core.eval.schema import load_gold, resolve_spec
    from pmi_core.eval.structural import STRUCTURAL_VOTERS
    from pmi_core.eval.systems import (
        labeller_predictions,
        labeller_sources,
        live_predictions,
        structural_predictions,
    )

    spec = resolve_spec(taxonomy)
    unknown = [
        s for s in systems if s not in ("live", "structural") and not s.startswith("labeller:")
    ]
    if unknown:
        raise click.ClickException(
            f"unknown system(s) {unknown} — expected 'live', 'structural', or 'labeller:<model id>'"
        )
    if "structural" in systems and spec.membership_question not in STRUCTURAL_VOTERS:
        # Scoring an all-abstain rule would post 0 recall and read as a finding
        # about the taxonomy rather than about the missing rule. On the GDP
        # board the omission is deliberate — the deterministic rule there IS the
        # live system, see structural.STRUCTURAL_VOTERS.
        click.echo(
            f"NOTE: no structural voter for '{spec.membership_question}' — "
            "skipping the 'structural' system.",
            err=True,
        )
        systems = tuple(s for s in systems if s != "structural")
        if not systems:
            raise click.ClickException("nothing left to score")
    meta, items = load_gold(spec)
    if not any(it.status == "labelled" for it in items):
        raise click.ClickException(
            f"no labelled items in the {spec.taxonomy} gold set — run `eval label` first"
        )

    out: dict[str, Any] = {"taxonomy": spec.taxonomy, "gold_items": len(items)}
    if exclude_labellers:
        items, readjudicated = readjudicate(spec, items, exclude_labellers)
        out["readjudicated"] = readjudicated
        if not any(it.status == "labelled" for it in items):
            raise click.ClickException(
                f"excluding {list(exclude_labellers)} leaves no item with two agreeing "
                "voters — label with another independent model before scoring this way"
            )

    reports = {}
    for name in systems:
        if name == "live":
            preds, diag = asyncio.run(
                live_predictions(spec, [it.market_id for it in items])
            )
            out["live_diagnostics"] = diag
        elif name.startswith("labeller:"):
            source = name.split(":", 1)[1]
            preds = labeller_predictions(spec, items, source)
            if not preds:
                raise click.ClickException(
                    f"no stored votes from '{source}'. Sources in this gold set: "
                    f"{labeller_sources(spec, items)}"
                )
        else:
            preds = structural_predictions(spec, items)
        rep = evaluate_membership(spec, meta, items, preds, system=name)
        if not errors:
            rep.errors = []
        reports[name] = rep
        out[name] = rep.as_dict()

    if "live" in reports and "structural" in reports:
        out["structural_vs_live"] = compare(reports["live"], reports["structural"])
    click.echo(json.dumps(out, indent=2, default=str))


@cli.group("models")
def models() -> None:
    """Manage factor model bundles (prompt + LLM + temperature + tools)."""


@models.command("list")
@click.option("--factor", default=None, help="Filter by factor_id.")
def models_list(factor: str | None) -> None:
    """List every CoreFactorModel row."""
    rows = asyncio.run(_models_list_async(factor))
    click.echo(json.dumps(rows, indent=2))


@models.command("doctor")
@click.option(
    "--strict/--no-strict",
    default=False,
    help="Exit non-zero when anything is unbound or orphaned (for cron / CI).",
)
def models_doctor(strict: bool) -> None:
    """Report factors running on the stub and registry rows nothing references.

    Falling back to the deterministic stub when no production row exists is by
    design (§6.1) and is how the e2e suite runs at $0 — but nothing distinguishes
    it from a working index, which is how the S&P shipped a hash-decided
    membership for eight days after a factor rename. This is the check that
    tells you; `--strict` is the one you put on a cron.
    """
    report = asyncio.run(_models_doctor_async())
    click.echo(json.dumps(report, indent=2))
    if strict and not report["ok"]:
        raise SystemExit(1)


@models.command("register")
@click.option("--factor", "factor_id", required=True)
@click.option("--prompt-name", required=True, help="e.g. factors/directly_about_war")
@click.option("--prompt-version", required=True, type=int)
@click.option(
    "--llm",
    "llm_model_id",
    required=True,
    help="e.g. gpt-4o-mini-2024-07-18, or agentic/gpt-4o for a Tier 2 tool-loop model.",
)
@click.option("--temperature", default=None, type=float)
@click.option(
    "--tools-config",
    default=None,
    help=(
        'JSON for Tier 2 agentic models, e.g. \'{"tools":["recent_trades"],'
        '"max_steps":4}\'. Ignored by single-shot (gpt-*/ollama/*) models.'
    ),
)
@click.option("--description", default=None)
@click.option("--created-by", default=None)
@click.option(
    "--no-mlflow",
    is_flag=True,
    default=False,
    help="Skip reserving an MLflow Model Registry name.",
)
def models_register(
    factor_id: str,
    prompt_name: str,
    prompt_version: int,
    llm_model_id: str,
    temperature: float | None,
    tools_config: str | None,
    description: str | None,
    created_by: str | None,
    no_mlflow: bool,
) -> None:
    """Register a new factor model in 'staging'. Use `models promote` to activate."""
    parsed_tools_config: dict | None = None
    if tools_config:
        try:
            parsed_tools_config = json.loads(tools_config)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"--tools-config is not valid JSON: {exc}") from exc
        if not isinstance(parsed_tools_config, dict):
            raise click.ClickException("--tools-config must be a JSON object")
    result = asyncio.run(
        _models_register_async(
            factor_id=factor_id,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            llm_model_id=llm_model_id,
            temperature=temperature,
            description=description,
            created_by=created_by,
            mlflow_register=not no_mlflow,
            tools_config=parsed_tools_config,
        )
    )
    click.echo(json.dumps(result, indent=2))


@models.command("promote")
@click.argument("model_id", type=int)
@click.option(
    "--stage",
    type=click.Choice(["staging", "production", "archived"]),
    default="production",
)
def models_promote(model_id: int, stage: str) -> None:
    """Promote a factor model to STAGE; demotes any other active row in same slot."""
    result = asyncio.run(_models_promote_async(model_id, stage))
    click.echo(json.dumps(result, indent=2))


# ──────────────────────────────────────────────────────────────────────────
# API keys — mint / list / revoke / rotate (CORR-0.7)
# ──────────────────────────────────────────────────────────────────────────
#
# pmi-api authenticates `X-API-Key: <raw>` by sha256-comparing against
# core_api_keys.key_hash (see pmi-api/pmi_api/deps.py). Raw keys are NEVER
# stored — only the hash + a short display prefix — so `create` / `rotate`
# print the raw token exactly once. Persist it immediately (e.g. into the
# pmi-web service env as PMI_API_KEY, or hand to an external consumer).

_KEY_PREFIX = "pmi_"


def _mint_raw_key() -> tuple[str, str, str]:
    """Return (raw_key, key_prefix, key_hash). Raw is shown once, never stored."""
    raw = _KEY_PREFIX + secrets.token_urlsafe(32)
    key_prefix = raw[:12]  # fits String(16); enough to eyeball in `keys list`
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, key_prefix, key_hash


async def _keys_create_async(label: str | None, rate_limit: int) -> dict:
    from pmi_core.models import CoreApiKey

    raw, key_prefix, key_hash = _mint_raw_key()
    async with session_scope() as session:
        row = CoreApiKey(
            key_prefix=key_prefix,
            key_hash=key_hash,
            label=label,
            rate_limit_per_minute=rate_limit,
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return {
            "id": row.id,
            "api_key": raw,  # shown ONCE — store it now
            "key_prefix": key_prefix,
            "label": label,
            "rate_limit_per_minute": rate_limit,
            "is_active": True,
        }


async def _keys_list_async() -> list[dict]:
    from pmi_core.models import CoreApiKey

    async with session_scope() as session:
        rows = (
            await session.execute(select(CoreApiKey).order_by(CoreApiKey.id))
        ).scalars().all()
        return [
            {
                "id": r.id,
                "key_prefix": r.key_prefix,
                "label": r.label,
                "rate_limit_per_minute": r.rate_limit_per_minute,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            }
            for r in rows
        ]


async def _keys_revoke_async(key_id: int) -> dict:
    from pmi_core.models import CoreApiKey

    async with session_scope() as session:
        row = (
            await session.execute(select(CoreApiKey).where(CoreApiKey.id == key_id))
        ).scalar_one_or_none()
        if row is None:
            raise click.ClickException(f"No CoreApiKey with id={key_id}")
        row.is_active = False
        await session.flush()
        return {"id": row.id, "key_prefix": row.key_prefix, "is_active": row.is_active}


async def _keys_rotate_async(key_id: int) -> dict:
    """Revoke an existing key and mint a replacement carrying its label + limit."""
    from pmi_core.models import CoreApiKey

    async with session_scope() as session:
        old = (
            await session.execute(select(CoreApiKey).where(CoreApiKey.id == key_id))
        ).scalar_one_or_none()
        if old is None:
            raise click.ClickException(f"No CoreApiKey with id={key_id}")
        old.is_active = False

        raw, key_prefix, key_hash = _mint_raw_key()
        new = CoreApiKey(
            key_prefix=key_prefix,
            key_hash=key_hash,
            label=old.label,
            rate_limit_per_minute=old.rate_limit_per_minute,
            is_active=True,
        )
        session.add(new)
        await session.flush()
        return {
            "rotated_from": {"id": old.id, "key_prefix": old.key_prefix},
            "id": new.id,
            "api_key": raw,  # shown ONCE
            "key_prefix": key_prefix,
            "label": new.label,
            "rate_limit_per_minute": new.rate_limit_per_minute,
            "is_active": True,
        }


# ──────────────────────────────────────────────────────────────────────────
# demo — score the fixture world without an LLM (fixture/ provider)
# ──────────────────────────────────────────────────────────────────────────

DEMO_MODEL_ID = "fixture/labels"
DEMO_INDEXES = ("war-index-2026", "btc-bull-bear-index", "us-senate-2026-republican-seats")


@cli.group("demo")
def demo() -> None:
    """Fixture-backed demo: bind factors to fixture labels, replay a history."""


async def _demo_bind_async(model_id: str) -> dict:
    from pmi_core.dsl.ir import load_index_def
    from pmi_core.engine.pipeline import ensure_prompt
    from pmi_core.models import CoreFactorModel

    prompt_refs: dict[str, str] = {}
    for yaml_path in sorted((REPO_ROOT / "pmi_core" / "index_defs").glob("*.yaml")):
        ir, _raw, _sha = load_index_def(yaml_path)
        for f in ir.factors:
            prompt_refs.setdefault(f.id, f.prompt_ref)

    bound, kept = [], []
    async with session_scope() as session:
        active = {
            r.factor_id
            for r in (
                await session.execute(
                    select(CoreFactorModel).where(
                        CoreFactorModel.stage == "production",
                        CoreFactorModel.is_active.is_(True),
                    )
                )
            ).scalars()
        }
        for factor_id, prompt_ref in sorted(prompt_refs.items()):
            # Never displace a real binding: the demo only fills gaps.
            if factor_id in active:
                kept.append(factor_id)
                continue
            prompt = await ensure_prompt(session, prompt_ref)
            version = (
                await session.execute(
                    select(CoreFactorModel.version)
                    .where(CoreFactorModel.factor_id == factor_id)
                    .order_by(CoreFactorModel.version.desc())
                    .limit(1)
                )
            ).scalar() or 0
            session.add(
                CoreFactorModel(
                    factor_id=factor_id,
                    version=version + 1,
                    prompt_id=prompt.id,
                    llm_model_id=model_id,
                    temperature=0.0,
                    stage="production",
                    is_active=True,
                    description="demo: replays fixture judgements",
                    created_by="pmi-core demo bind",
                )
            )
            bound.append(factor_id)
    return {"model": model_id, "bound": bound, "kept_existing": kept}


@demo.command("bind")
@click.option("--model", "model_id", default=DEMO_MODEL_ID, show_default=True)
def demo_bind(model_id: str) -> None:
    """Bind every factor without a production model to the fixture provider."""
    click.echo(json.dumps(asyncio.run(_demo_bind_async(model_id)), indent=2))


async def _demo_history_async(index_ids: tuple[str, ...], days: int) -> list[dict]:
    from pmi_core.engine import run_pipeline
    from pmi_core.engine.pipeline import backfill_scores

    now = datetime.now(UTC).replace(microsecond=0)
    since = now - timedelta(days=days, hours=1)
    out = []
    for index_id in index_ids:
        # One tick per day, stamped in the past. run_pipeline prices at "now";
        # backfill then re-prices every stored point at its own as_of from the
        # seeded snapshot history, so the series follows the fixture prices.
        for d in range(days, 0, -1):
            await run_pipeline(index_id=index_id, as_of=now - timedelta(days=d))
        latest = await run_pipeline(index_id=index_id, as_of=now)
        backfill = await backfill_scores(index_id=index_id, since=since)
        out.append(
            {
                "index_id": index_id,
                "score": latest.get("score"),
                "component_count": latest.get("component_count"),
                "points": backfill.get("points"),
            }
        )
    return out


@demo.command("history")
@click.argument("index_ids", nargs=-1)
@click.option("--days", default=14, show_default=True)
def demo_history(index_ids: tuple[str, ...], days: int) -> None:
    """Score INDEX_IDS once per day for the last --days (default: the demo set)."""
    rows = asyncio.run(_demo_history_async(index_ids or DEMO_INDEXES, days))
    click.echo(json.dumps(rows, indent=2, default=str))


@cli.group("keys")
def keys() -> None:
    """Manage pmi-api access keys (X-API-Key)."""


@keys.command("create")
@click.option("--label", default=None, help="Human label, e.g. 'pmi-web' or 'acme-corp'.")
@click.option("--rate-limit", default=60, type=int, help="rate_limit_per_minute (default 60).")
def keys_create(label: str | None, rate_limit: int) -> None:
    """Mint a new API key. Prints the raw token ONCE — store it now."""
    result = asyncio.run(_keys_create_async(label, rate_limit))
    click.echo(json.dumps(result, indent=2))
    click.echo("\n⚠  Save `api_key` now — it is not recoverable.", err=True)


@keys.command("list")
def keys_list() -> None:
    """List API keys (prefix + metadata only; never the raw token)."""
    rows = asyncio.run(_keys_list_async())
    click.echo(json.dumps(rows, indent=2))


@keys.command("revoke")
@click.argument("key_id", type=int)
def keys_revoke(key_id: int) -> None:
    """Deactivate the key with KEY_ID (sets is_active=False)."""
    result = asyncio.run(_keys_revoke_async(key_id))
    click.echo(json.dumps(result, indent=2))


@keys.command("rotate")
@click.argument("key_id", type=int)
def keys_rotate(key_id: int) -> None:
    """Revoke KEY_ID and mint a replacement with the same label + rate limit."""
    result = asyncio.run(_keys_rotate_async(key_id))
    click.echo(json.dumps(result, indent=2))
    click.echo("\n⚠  Save the new `api_key` now — it is not recoverable.", err=True)


if __name__ == "__main__":
    cli()
