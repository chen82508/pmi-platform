"""core_jobs — Postgres-backed job queue (CORR-4.6, Redis-free variant).

Fills the Arq role from CLAUDE.md §7 without introducing Redis: fire-and-forget
short tasks, WS-triggered single-market re-evals, and the §3.2 on-demand score
path all enqueue here. Workers claim with ``FOR UPDATE SKIP LOCKED`` (safe for
N parallel worker containers) and wake on ``pg_notify('pmi_jobs')``.

Lifecycle: queued → running → succeeded | failed (or back to queued on retry /
stale-heartbeat recovery). ``dedupe_key`` collapses duplicate *pending* work —
a partial unique index over status IN ('queued','running') means "score
polymarket-war-index" enqueued ten times during a trade storm runs once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreJob(Base):
    __tablename__ = "core_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Registered job name (pmi_workers.registry) the worker dispatches to.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # kwargs passed to the job function. Must be JSON-serializable.
    args: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    queue: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # Lower = claimed sooner. On-demand API requests enqueue at 50, cron at 100.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Not claimable before this — doubles as the retry-backoff mechanism.
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # NULL = no dedupe. Uniqueness is enforced only while queued/running (see
    # partial index below), so a finished job never blocks a re-enqueue.
    dedupe_key: Mapped[str | None] = mapped_column(String(256))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Touched every PMI_WORKER_HEARTBEAT_SEC by the executing worker; a running
    # job whose heartbeat goes stale is presumed crashed and re-queued.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))

    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_core_jobs__claim", "status", "queue", "run_at", "priority"),
        Index(
            "uq_core_jobs__dedupe_pending",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )
