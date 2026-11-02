"""`pmi-workers` Click CLI.

Local debug surface. The production cron entry point is `enqueue <name>`; `run <name>` / `score <index_id>` execute a job inline
(see [`pmi_workers.runner`](runner.py)); this CLI is just sugar for humans:

    pmi-workers list                       # all registered jobs
    pmi-workers run <name>                 # run one by registered name
    pmi-workers score <index_id>           # shortcut for one PMI tick

P1 lands `pmi-workers arq` to start the Arq listener. Kept stubbed so the
help text already advertises the shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import click
import structlog
from pmi_core.config import settings

from pmi_workers import registry
from pmi_workers.jobs.score import score_index


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
    """pmi-workers CLI."""
    _setup_logging()


@cli.command(name="list")
def list_jobs() -> None:
    """List every registered job name."""
    names = registry.all_names()
    if not names:
        click.echo("(no jobs registered)")
        return
    for n in names:
        click.echo(n)


@cli.command()
@click.argument("name")
def run(name: str) -> None:
    """Run a registered job by name."""
    try:
        job = registry.get(name)
    except KeyError:
        click.echo(f"error: '{name}' not registered. Try `pmi-workers list`.", err=True)
        sys.exit(2)
    asyncio.run(job())


@cli.command()
@click.argument("index_id")
def score(index_id: str) -> None:
    """Run one pipeline tick for INDEX_ID (shortcut over `run score:<id>`)."""
    result = asyncio.run(score_index(index_id))
    click.echo(json.dumps(result, indent=2, default=str))


@cli.command()
@click.option(
    "--queue",
    "queues",
    multiple=True,
    help="Only claim from these queues. Repeatable. Default: all queues.",
)
def worker(queues: tuple[str, ...]) -> None:
    """Start the Postgres-queue worker loop (CORR-4.6, Redis-free Arq role).

    Claims `core_jobs` rows (FOR UPDATE SKIP LOCKED) and executes the
    registered job they name. Safe to run several of these side by side.
    """
    from pmi_core.observability import init_sentry

    from pmi_workers.worker import run_worker

    init_sentry("pmi-workers")
    asyncio.run(run_worker(list(queues) or None))


@cli.command()
@click.argument("name")
@click.option("--args", "args_json", default=None, help="Job kwargs as a JSON object.")
@click.option(
    "--queue",
    "queue_name",
    default=None,
    help="Override the lane. Default: the one the job declared via @register.",
)
@click.option("--priority", default=100, show_default=True, help="Lower = sooner.")
@click.option(
    "--dedupe/--no-dedupe",
    default=True,
    show_default=True,
    help="Collapse with an identical already-pending job (key = name + args).",
)
def enqueue(
    name: str, args_json: str | None, queue_name: str | None, priority: int, dedupe: bool
) -> None:
    """Enqueue NAME onto the Postgres queue (the cron → worker entry point).

    Supercronic lines call this instead of executing in-process, so schedule
    beats never stack on a slow tick and all execution funnels through the
    worker (single place for retry / heartbeat / audit).
    """
    from pmi_core import queue as pgq

    from pmi_workers import jobs  # noqa: F401  — populates the registry
    from pmi_workers.registry import queue_for

    args = json.loads(args_json) if args_json else {}
    if not isinstance(args, dict):
        click.echo("error: --args must be a JSON object", err=True)
        sys.exit(2)
    dedupe_key = None
    if dedupe:
        canon = json.dumps(args, sort_keys=True) if args else ""
        dedupe_key = f"{name}:{canon}" if canon else name

    job = asyncio.run(
        pgq.enqueue_and_notify(
            name,
            args,
            queue=queue_name or queue_for(name),
            priority=priority,
            dedupe_key=dedupe_key,
        )
    )
    click.echo(json.dumps({"job_id": job.id, "name": job.name, "status": job.status}))


@cli.command()
@click.argument("index_id")
@click.option("--days", default=90, show_default=True)
@click.option("--step-hours", default=24, show_default=True)
@click.option(
    "--version",
    "version",
    type=int,
    default=None,
    help="Pin a specific index version to replay (default: current). Replay "
    "several versions over the same window to diff them in MLflow (SHIP-3.6).",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    show_default=True,
    help="Run inline to completion instead of leaving it to the worker.",
)
def backtest(
    index_id: str, days: int, step_hours: int, version: int | None, wait: bool
) -> None:
    """Start a durable backtest workflow for INDEX_ID (CORR-8.1 / SHIP-3.4).

    With --wait, executes in-process and prints the replay points as CSV;
    without, enqueues for the worker and prints the workflow_run_id to poll
    via GET /workflows/{id}. Either way the run is mirrored to MLflow.
    """
    from pmi_core import queue as pgq
    from pmi_core.db import session_scope
    from pmi_core.workflow import create_run, execute_run

    async def _start() -> None:
        async with session_scope() as session:
            args: dict = {
                "index_id": index_id,
                "days": days,
                "step_hours": step_hours,
            }
            if version is not None:  # absent key → workflow's current-def default
                args["version"] = version
            run = await create_run(session, "backtest", args)
            job_id = None
            if not wait:
                # Hand execution to the worker. With --wait we run inline
                # instead — enqueuing too would race a worker for the same run.
                job = await pgq.enqueue(
                    session,
                    "workflow",
                    {"workflow_run_id": run.id},
                    dedupe_key=f"workflow:{run.id}",
                )
                run.job_id = job.id
                job_id = job.id
                await pgq.notify(session)
            run_id = run.id

        if not wait:
            click.echo(json.dumps({"workflow_run_id": run_id, "job_id": job_id}))
            return

        result = await execute_run(run_id)
        mlflow_run_id = result.get("mlflow_run_id")
        if mlflow_run_id:
            click.echo(
                f"mlflow run {mlflow_run_id} (experiment {result.get('mlflow_experiment_id')}) "
                f"— version v{result.get('version')}",
                err=True,
            )
        click.echo("as_of,score,component_count,markets_priced")
        for p in result.get("points", []):
            click.echo(
                f"{p['as_of']},{p['score'] if p['score'] is not None else ''},"
                f"{p['component_count']},{p['markets_priced']}"
            )

    asyncio.run(_start())


if __name__ == "__main__":
    cli()
