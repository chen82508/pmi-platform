"""/jobs + /workflows — poll handles for the §3.2 on-demand path.

A 202 from `/indexes/{id}/score?max_age_s=...`, `/score/refresh`, or
`/backtest` carries a job_id / workflow_run_id; these endpoints are how the
caller (web, MCP, curl) watches it finish. Read-only — producers enqueue via
the indexes routes.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pmi_core.models import CoreJob, CoreWorkflowRun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key
from pmi_api.schemas import (
    JobEnvelope,
    JobPayload,
    WorkflowRunEnvelope,
    WorkflowRunPayload,
)

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.get("/jobs/{job_id}", response_model=JobEnvelope)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> JobEnvelope:
    job = (
        await session.execute(select(CoreJob).where(CoreJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "JOB_NOT_FOUND"}},
        )
    return JobEnvelope(
        summary=f"job {job.id} ({job.name}) is {job.status}",
        data=JobPayload.model_validate(job),
    )


async def _get_run(session: AsyncSession, workflow_run_id: int) -> CoreWorkflowRun:
    run = (
        await session.execute(
            select(CoreWorkflowRun).where(CoreWorkflowRun.id == workflow_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "WORKFLOW_NOT_FOUND"}},
        )
    return run


@router.get("/workflows/{workflow_run_id}", response_model=WorkflowRunEnvelope)
async def get_workflow(
    workflow_run_id: int, session: AsyncSession = Depends(get_session)
) -> WorkflowRunEnvelope:
    run = await _get_run(session, workflow_run_id)
    progress = (
        f"{run.steps_done}/{run.steps_total}" if run.steps_total else str(run.steps_done)
    )
    return WorkflowRunEnvelope(
        summary=(
            f"workflow {run.id} ({run.workflow}) is {run.status}, steps {progress}"
        ),
        data=WorkflowRunPayload.model_validate(run),
    )


@router.get("/workflows/{workflow_run_id}/csv", response_class=PlainTextResponse)
async def get_workflow_csv(
    workflow_run_id: int,
    session: AsyncSession = Depends(get_session),
    columns: str | None = Query(
        default=None, description="Comma-separated column subset; default all."
    ),
) -> PlainTextResponse:
    """Backtest result as CSV (SHIP-3.4's '一鍵 replay 出 CSV' contract).

    Works for any finished workflow whose result contains a `points` list of
    flat dicts; 409 while still running.
    """
    run = await _get_run(session, workflow_run_id)
    if run.status != "succeeded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "WORKFLOW_NOT_FINISHED",
                    "hint": f"status={run.status}; poll GET /workflows/{run.id}",
                }
            },
        )
    points = (run.result or {}).get("points") or []
    if not points:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NO_POINTS_IN_RESULT"}},
        )
    fieldnames = list(points[0].keys())
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        fieldnames = [c for c in requested if c in fieldnames] or fieldnames
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for p in points:
        writer.writerow(p)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")
