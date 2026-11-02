"""Durable workflow runner (CORR-8.1) — the Temporal role from §7, on Postgres.

A workflow is a registered async function ``async def wf(ctx, **args)`` executed
by the queue worker (job name ``workflow``, args ``{"workflow_run_id": N}``).
Durability comes from step checkpointing, not process supervision:

* ``await ctx.step(key, fn, *a, **kw)`` runs ``fn`` once per (run, key). The
  JSON result is persisted to ``core_workflow_steps``; on crash + queue retry
  the workflow function replays from the top and completed steps return their
  persisted result without re-executing — Temporal's event-sourced replay with
  Postgres rows as the event log.
* The replay contract: a workflow must derive the same step keys from the same
  ``args`` on every execution, and step results must be JSON-serializable.

What this deliberately does NOT have (vs real Temporal): signals, timers,
child workflows, multi-worker fan-out of one run. When those are needed,
that's the moment to revisit Temporal Cloud (§13 open question).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update

from pmi_core.db import session_scope
from pmi_core.models import CoreWorkflowRun, CoreWorkflowStep

log = structlog.get_logger(__name__)

WorkflowFn = Callable[..., Awaitable[Any]]

WORKFLOWS: dict[str, WorkflowFn] = {}


def workflow(name: str) -> Callable[[WorkflowFn], WorkflowFn]:
    """Decorator: register an async workflow function under ``name``."""

    def decorator(fn: WorkflowFn) -> WorkflowFn:
        if name in WORKFLOWS:
            raise RuntimeError(f"Workflow '{name}' already registered")
        WORKFLOWS[name] = fn
        return fn

    return decorator


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class WorkflowContext:
    """Handed to the workflow function; owns checkpoint reads/writes.

    Each checkpoint uses its own short transaction so a completed step is
    durable the moment it returns — the surrounding workflow holds no
    long-lived DB transaction (a 90-day backtest must not pin one connection
    for its whole runtime).
    """

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id

    async def step(self, key: str, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        async with session_scope() as session:
            existing = (
                await session.execute(
                    select(CoreWorkflowStep).where(
                        CoreWorkflowStep.workflow_run_id == self.run_id,
                        CoreWorkflowStep.step_key == key,
                        CoreWorkflowStep.status == "succeeded",
                    )
                )
            ).scalar_one_or_none()
        if existing is not None:
            return (existing.result or {}).get("value")

        started = datetime.now(UTC)
        value = await fn(*args, **kwargs)

        async with session_scope() as session:
            session.add(
                CoreWorkflowStep(
                    workflow_run_id=self.run_id,
                    step_key=key,
                    status="succeeded",
                    # Wrap so a step legitimately returning None is still
                    # distinguishable from "no result column".
                    result={"value": _json_safe(value)},
                    started_at=started,
                    finished_at=datetime.now(UTC),
                )
            )
            await session.execute(
                update(CoreWorkflowRun)
                .where(CoreWorkflowRun.id == self.run_id)
                .values(steps_done=CoreWorkflowRun.steps_done + 1)
            )
        return value

    async def set_steps_total(self, total: int) -> None:
        """Optional progress hint for the API/UI."""
        async with session_scope() as session:
            await session.execute(
                update(CoreWorkflowRun)
                .where(CoreWorkflowRun.id == self.run_id)
                .values(steps_total=total)
            )


async def create_run(session, workflow_name: str, args: dict | None = None) -> CoreWorkflowRun:
    """Insert a workflow run row (status=queued). The caller is responsible
    for enqueuing the executing ``workflow`` job and committing."""
    if workflow_name not in WORKFLOWS:
        # Producers (pmi-api) may not import the module that registers the
        # workflow — only the worker must be able to resolve it. Validate
        # against the known-names list instead when the registry looks empty.
        log.debug("workflow.create_unverified", workflow=workflow_name)
    run = CoreWorkflowRun(workflow=workflow_name, args=args or {}, status="queued")
    session.add(run)
    await session.flush()
    return run


async def execute_run(workflow_run_id: int) -> dict:
    """Execute (or resume) one workflow run to completion. Called by the
    queue's ``workflow`` job — queue retry semantics make this durable:
    an exception re-queues the job with backoff, and the next execution
    replays from the last checkpoint."""
    # Late import so registering modules (engine.backtest) are loaded even
    # when execute_run is reached through a bare worker process.
    import pmi_core.engine.backtest  # noqa: F401

    async with session_scope() as session:
        run = (
            await session.execute(
                select(CoreWorkflowRun).where(CoreWorkflowRun.id == workflow_run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"workflow run {workflow_run_id} not found")
        if run.status == "succeeded":
            return run.result or {}
        if run.status == "cancelled":
            return {"cancelled": True}
        fn = WORKFLOWS.get(run.workflow)
        if fn is None:
            raise ValueError(f"workflow '{run.workflow}' not registered")
        args = dict(run.args or {})
        await session.execute(
            update(CoreWorkflowRun)
            .where(CoreWorkflowRun.id == workflow_run_id)
            .values(status="running", started_at=run.started_at or datetime.now(UTC))
        )

    ctx = WorkflowContext(workflow_run_id)
    try:
        result = await fn(ctx, **args)
    except Exception as exc:
        async with session_scope() as session:
            await session.execute(
                update(CoreWorkflowRun)
                .where(CoreWorkflowRun.id == workflow_run_id)
                .values(status="failed", error=repr(exc)[:4000])
            )
        log.error("workflow.failed", run_id=workflow_run_id, error=str(exc)[:300])
        raise

    safe_result = _json_safe(result) if isinstance(result, dict) else {"value": _json_safe(result)}
    async with session_scope() as session:
        await session.execute(
            update(CoreWorkflowRun)
            .where(CoreWorkflowRun.id == workflow_run_id)
            .values(
                status="succeeded",
                finished_at=datetime.now(UTC),
                result=safe_result,
                error=None,
            )
        )
    log.info("workflow.done", run_id=workflow_run_id)
    return safe_result
