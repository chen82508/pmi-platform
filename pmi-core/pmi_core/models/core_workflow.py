"""core_workflow_runs / core_workflow_steps — durable workflows (CORR-8.1, Postgres-based).

Fills the Temporal role from CLAUDE.md §7 without standing up Temporal: a
workflow is a named async function executed by the queue worker (via a
``workflow`` job carrying ``workflow_run_id``) that checkpoints every step into
``core_workflow_steps``. On crash/retry the function replays from the top, but
``WorkflowContext.step()`` returns the persisted result for already-completed
steps instantly — the Temporal event-sourcing idea, with Postgres rows as the
event log. A 2-hour backtest that dies at day 60/90 resumes at day 61.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreWorkflowRun(Base):
    __tablename__ = "core_workflow_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Registered workflow name (pmi_core.workflow.WORKFLOWS), e.g. 'backtest'.
    workflow: Mapped[str] = mapped_column(String(128), nullable=False)
    args: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # The core_jobs row executing this run (latest attempt). Retries reuse the
    # same job id (the queue re-queues the row rather than cloning it).
    job_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core_jobs.id"))

    # Progress for the API/UI: steps_done/steps_total, updated as steps commit.
    steps_total: Mapped[int | None] = mapped_column(Integer)
    steps_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoreWorkflowStep(Base):
    __tablename__ = "core_workflow_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_workflow_runs.id"), nullable=False
    )
    # Deterministic per-step key, e.g. 'day:2026-05-01'. The replay contract:
    # the workflow function must derive the same keys from the same args.
    step_key: Mapped[str] = mapped_column(String(256), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    result: Mapped[dict | None] = mapped_column(JSON)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id", "step_key", name="uq_core_workflow_steps__run_step"
        ),
        Index("ix_core_workflow_steps__run", "workflow_run_id"),
    )
