"""Postgres-backed job queue (CORR-4.6) — the Arq role from §7, without Redis.

Design (2026-06-11 decision: Postgres is the only stateful infra on the EC2 box):

* ``core_jobs`` is the queue. Producers (`pmi-api` on-demand path, supercronic
  via ``pmi-workers enqueue``, the WS consumer) INSERT; workers claim with
  ``FOR UPDATE SKIP LOCKED`` so any number of worker containers can run
  side-by-side without double-execution.
* ``pg_notify('pmi_jobs')`` after enqueue wakes idle workers instantly;
  polling (``PMI_WORKER_POLL_INTERVAL_SEC``) is the fallback when the LISTEN
  connection is unavailable.
* ``dedupe_key`` + a partial unique index over queued/running rows collapses
  duplicate pending work ("score X" during a trade storm runs once).
* Retry = re-queue with exponential ``run_at`` backoff until ``max_attempts``.
  A job that raises :class:`PermanentJobError` skips retry and goes terminal
  immediately — see that class for why batch jobs need it.
* Crash recovery = ``requeue_stale``: running rows whose heartbeat went silent
  are re-queued (or failed once attempts are exhausted).

Every function here takes the caller's ``AsyncSession`` and does NOT commit —
transaction boundaries belong to the caller (worker loop / API route).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.config import settings
from pmi_core.models import CoreJob

log = structlog.get_logger(__name__)

NOTIFY_CHANNEL = "pmi_jobs"

# Priorities: lower claims sooner. API on-demand beats cron batch work.
PRIORITY_INTERACTIVE = 50
PRIORITY_DEFAULT = 100


class PermanentJobError(Exception):
    """A failure that re-running the job cannot fix — fail terminally, no retry.

    Retry only pays off for *transient* faults (DB blip, LLM timeout, dead
    worker). For a deterministic fault the retry re-does all the work and fails
    again at the same place, which is worse than useless on a fan-out batch job:
    a single permanently-broken index made `score-all` re-score all 11 indexes
    three times per beat, saturating the worker for hours with work whose result
    was already known.
    """


def _json_safe(value: dict | None) -> dict:
    """Round-trip through json so non-serializable values fail at enqueue
    time (clear stack) instead of inside asyncpg (opaque)."""
    if not value:
        return {}
    return json.loads(json.dumps(value, default=str))


async def enqueue(
    session: AsyncSession,
    name: str,
    args: dict | None = None,
    *,
    queue: str = "default",
    dedupe_key: str | None = None,
    priority: int = PRIORITY_DEFAULT,
    run_at: datetime | None = None,
    max_attempts: int | None = None,
) -> CoreJob:
    """Insert a job; on a pending dedupe hit, return the existing row instead.

    The caller commits. After commit, call :func:`notify` (or use
    :func:`enqueue_and_notify`) so idle workers wake immediately.
    """
    values = {
        "name": name,
        "args": _json_safe(args),
        "queue": queue,
        "dedupe_key": dedupe_key,
        "priority": priority,
        "run_at": run_at or datetime.now(UTC),
        "max_attempts": max_attempts or settings.job_default_max_attempts,
    }
    stmt = pg_insert(CoreJob).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("status IN ('queued', 'running')"),
        )
    stmt = stmt.returning(CoreJob.id)
    new_id = (await session.execute(stmt)).scalar_one_or_none()

    if new_id is None:
        # Dedupe hit — surface the already-pending job.
        existing = (
            await session.execute(
                select(CoreJob)
                .where(
                    CoreJob.dedupe_key == dedupe_key,
                    CoreJob.status.in_(("queued", "running")),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.debug("queue.dedupe_hit", name=name, dedupe_key=dedupe_key, job_id=existing.id)
            return existing
        # Race: the pending twin finished between our INSERT and SELECT.
        # Retry the plain insert — at-least-once beats losing the request.
        stmt = pg_insert(CoreJob).values(**values).returning(CoreJob.id)
        new_id = (await session.execute(stmt)).scalar_one()

    job = (await session.execute(select(CoreJob).where(CoreJob.id == new_id))).scalar_one()
    log.info("queue.enqueued", name=name, job_id=job.id, queue=queue, dedupe_key=dedupe_key)
    return job


async def notify(session: AsyncSession) -> None:
    """Wake idle workers. NOTIFY is transactional in Postgres — it is
    delivered at commit, so calling this in the same transaction as the
    enqueue is correct (listeners never see a not-yet-visible job)."""
    await session.execute(text(f"SELECT pg_notify('{NOTIFY_CHANNEL}', '')"))


async def enqueue_and_notify(name: str, args: dict | None = None, **kwargs) -> CoreJob:
    """Convenience for producers without their own session/transaction."""
    from pmi_core.db import session_scope

    async with session_scope() as session:
        job = await enqueue(session, name, args, **kwargs)
        await notify(session)
    return job


async def claim_next(
    session: AsyncSession, *, worker_id: str, queues: list[str] | None = None
) -> CoreJob | None:
    """Claim the most urgent runnable job, or None. SKIP LOCKED — concurrent
    workers never claim the same row. Caller commits to release the lock."""
    candidate = (
        select(CoreJob.id)
        .where(CoreJob.status == "queued", CoreJob.run_at <= datetime.now(UTC))
        .order_by(CoreJob.priority, CoreJob.run_at, CoreJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if queues:
        candidate = candidate.where(CoreJob.queue.in_(queues))

    now = datetime.now(UTC)
    stmt = (
        update(CoreJob)
        .where(CoreJob.id.in_(candidate.scalar_subquery()))
        .values(
            status="running",
            started_at=now,
            heartbeat_at=now,
            worker_id=worker_id,
            attempts=CoreJob.attempts + 1,
        )
        .returning(CoreJob)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row


async def complete(session: AsyncSession, job_id: int, result: dict | None = None) -> None:
    await session.execute(
        update(CoreJob)
        .where(CoreJob.id == job_id)
        .values(
            status="succeeded",
            finished_at=datetime.now(UTC),
            result=_json_safe(result) if result else None,
            error=None,
        )
    )


async def fail(
    session: AsyncSession, job: CoreJob, error: str, *, retryable: bool = True
) -> str:
    """Mark a claimed job failed; re-queue with backoff while attempts remain.

    ``retryable=False`` (the caller saw a :class:`PermanentJobError`) goes
    terminal on the first failure instead of burning the remaining attempts on
    work that cannot succeed.

    Returns the resulting status ('queued' for retry, 'failed' for terminal).
    """
    now = datetime.now(UTC)
    if retryable and job.attempts < job.max_attempts:
        backoff = min(
            settings.job_retry_backoff_base_sec * (2 ** (job.attempts - 1)), 600.0
        )
        await session.execute(
            update(CoreJob)
            .where(CoreJob.id == job.id)
            .values(
                status="queued",
                run_at=now + timedelta(seconds=backoff),
                error=error[:4000],
            )
        )
        log.warning(
            "queue.retrying", job_id=job.id, name=job.name,
            attempt=job.attempts, max_attempts=job.max_attempts, backoff_sec=backoff,
        )
        return "queued"
    await session.execute(
        update(CoreJob)
        .where(CoreJob.id == job.id)
        .values(status="failed", finished_at=now, error=error[:4000])
    )
    log.error("queue.failed_terminal", job_id=job.id, name=job.name, error=error[:300])
    return "failed"


async def heartbeat(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        update(CoreJob)
        .where(CoreJob.id == job_id, CoreJob.status == "running")
        .values(heartbeat_at=datetime.now(UTC))
    )


async def requeue_stale(session: AsyncSession) -> int:
    """Recover jobs whose worker died: heartbeat older than
    ``PMI_WORKER_STALE_AFTER_SEC`` → back to queued (attempts already counted
    at claim time, so a crash-looping job still exhausts max_attempts).
    Rows out of attempts go terminal-failed instead."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker_stale_after_sec)

    failed = (
        await session.execute(
            update(CoreJob)
            .where(
                CoreJob.status == "running",
                CoreJob.heartbeat_at < cutoff,
                CoreJob.attempts >= CoreJob.max_attempts,
            )
            .values(
                status="failed",
                finished_at=datetime.now(UTC),
                error="stale heartbeat: worker presumed dead, attempts exhausted",
            )
            .returning(CoreJob.id)
        )
    ).scalars().all()

    requeued = (
        await session.execute(
            update(CoreJob)
            .where(CoreJob.status == "running", CoreJob.heartbeat_at < cutoff)
            .values(status="queued", run_at=datetime.now(UTC), worker_id=None)
            .returning(CoreJob.id)
        )
    ).scalars().all()

    n = len(requeued)
    if n or failed:
        log.warning("queue.requeued_stale", requeued=n, failed=len(failed))
    return n


async def get_job(session: AsyncSession, job_id: int) -> CoreJob | None:
    return (
        await session.execute(select(CoreJob).where(CoreJob.id == job_id))
    ).scalar_one_or_none()
