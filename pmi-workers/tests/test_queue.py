"""pmi_core.queue behavioural tests — enqueue / dedupe / claim / retry / stale."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core import queue
from pmi_core.models import CoreJob
from sqlalchemy import update


async def test_enqueue_claim_complete_roundtrip(session_factory):
    async with session_factory() as s:
        job = await queue.enqueue(s, "score", {"index_id": "x"})
        await s.commit()
        job_id = job.id

    async with session_factory() as s:
        claimed = await queue.claim_next(s, worker_id="t-worker")
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == "running"
        assert claimed.attempts == 1
        assert claimed.worker_id == "t-worker"
        await s.commit()

    # Queue is now empty for further claims.
    async with session_factory() as s:
        assert await queue.claim_next(s, worker_id="t-worker") is None

    async with session_factory() as s:
        await queue.complete(s, job_id, {"score": 55.0})
        await s.commit()

    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "succeeded"
        assert row.result == {"score": 55.0}
        assert row.finished_at is not None


async def test_dedupe_collapses_pending_then_allows_after_finish(session_factory):
    async with session_factory() as s:
        first = await queue.enqueue(s, "score", {"index_id": "x"}, dedupe_key="score:x")
        second = await queue.enqueue(s, "score", {"index_id": "x"}, dedupe_key="score:x")
        await s.commit()
        assert first.id == second.id

    # Dedupe also holds across transactions while pending.
    async with session_factory() as s:
        third = await queue.enqueue(s, "score", {"index_id": "x"}, dedupe_key="score:x")
        await s.commit()
        assert third.id == first.id

    async with session_factory() as s:
        claimed = await queue.claim_next(s, worker_id="t")
        await queue.complete(s, claimed.id, None)
        await s.commit()

    # Finished job no longer blocks the key.
    async with session_factory() as s:
        fourth = await queue.enqueue(s, "score", {"index_id": "x"}, dedupe_key="score:x")
        await s.commit()
        assert fourth.id != first.id


async def test_claim_respects_priority_then_fifo(session_factory):
    async with session_factory() as s:
        cron = await queue.enqueue(s, "hourly", priority=queue.PRIORITY_DEFAULT)
        ondemand = await queue.enqueue(s, "score", priority=queue.PRIORITY_INTERACTIVE)
        await s.commit()
        cron_id, ondemand_id = cron.id, ondemand.id

    async with session_factory() as s:
        first = await queue.claim_next(s, worker_id="t")
        await s.commit()
    async with session_factory() as s:
        second = await queue.claim_next(s, worker_id="t")
        await s.commit()

    assert first.id == ondemand_id  # interactive (50) beats cron (100)
    assert second.id == cron_id


async def test_retry_backoff_then_terminal_failure(session_factory):
    async with session_factory() as s:
        job = await queue.enqueue(s, "score", max_attempts=2)
        await s.commit()
        job_id = job.id

    async with session_factory() as s:
        claimed = await queue.claim_next(s, worker_id="t")
        outcome = await queue.fail(s, claimed, "boom 1")
        await s.commit()
        assert outcome == "queued"

    # Re-queued with future run_at (backoff) — not claimable yet.
    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "queued"
        assert row.run_at > datetime.now(UTC)
        assert await queue.claim_next(s, worker_id="t") is None
        await s.commit()

    # Force the backoff window past and claim again → second attempt.
    async with session_factory() as s:
        await s.execute(
            update(CoreJob)
            .where(CoreJob.id == job_id)
            .values(run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    async with session_factory() as s:
        claimed = await queue.claim_next(s, worker_id="t")
        assert claimed.attempts == 2
        outcome = await queue.fail(s, claimed, "boom 2")
        await s.commit()
        assert outcome == "failed"

    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "failed"
        assert "boom 2" in row.error


async def test_permanent_failure_goes_terminal_without_burning_attempts(session_factory):
    """A deterministic fault must not consume the retry budget: re-running would
    fail at the same place, and for a fan-out job like score-all each retry
    re-does every sibling index too."""
    async with session_factory() as s:
        job = await queue.enqueue(s, "score", max_attempts=3)
        await s.commit()
        job_id = job.id

    async with session_factory() as s:
        claimed = await queue.claim_next(s, worker_id="t")
        outcome = await queue.fail(s, claimed, "no such yaml", retryable=False)
        await s.commit()
        assert outcome == "failed"

    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "failed"
        assert row.finished_at is not None
        assert row.attempts == 1  # attempts 2 and 3 never spent
        assert await queue.claim_next(s, worker_id="t") is None
        await s.commit()


async def test_requeue_stale_recovers_crashed_worker(session_factory):
    async with session_factory() as s:
        job = await queue.enqueue(s, "score")
        await s.commit()
        job_id = job.id

    async with session_factory() as s:
        await queue.claim_next(s, worker_id="t")
        await s.commit()

    # Simulate a dead worker: heartbeat far in the past.
    async with session_factory() as s:
        await s.execute(
            update(CoreJob)
            .where(CoreJob.id == job_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()

    async with session_factory() as s:
        n = await queue.requeue_stale(s)
        await s.commit()
        assert n == 1

    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "queued"
        # Attempt budget was consumed by the crashed run.
        assert row.attempts == 1
        reclaimed = await queue.claim_next(s, worker_id="t2")
        assert reclaimed.id == job_id
        assert reclaimed.attempts == 2
        await s.commit()


async def test_stale_job_out_of_attempts_goes_terminal(session_factory):
    async with session_factory() as s:
        job = await queue.enqueue(s, "score", max_attempts=1)
        await s.commit()
        job_id = job.id

    async with session_factory() as s:
        await queue.claim_next(s, worker_id="t")
        await s.commit()
    async with session_factory() as s:
        await s.execute(
            update(CoreJob)
            .where(CoreJob.id == job_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()

    async with session_factory() as s:
        n = await queue.requeue_stale(s)
        await s.commit()
        assert n == 0

    async with session_factory() as s:
        row = await queue.get_job(s, job_id)
        assert row.status == "failed"
        assert "stale heartbeat" in row.error
