"""Long-running queue worker (CORR-4.6) — claims `core_jobs`, runs registry jobs.

Started via `pmi-workers worker` (compose service `pmi-worker`). The cron →
worker split from §3.2/§7: supercronic *enqueues* (cheap INSERT), this loop
*executes*. Multiple worker containers are safe — claims use FOR UPDATE SKIP
LOCKED (see pmi_core.queue).

Loop shape:
* LISTEN on pg_notify('pmi_jobs') over a dedicated asyncpg connection so an
  on-demand API request starts computing within milliseconds; falls back to
  PMI_WORKER_POLL_INTERVAL_SEC polling when the listener can't be established.
* Up to PMI_WORKER_CONCURRENCY jobs in flight (each pipeline tick is already
  internally concurrent, keep this small).
* A per-job heartbeat task touches `heartbeat_at`; the periodic
  `requeue_stale` sweep recovers jobs from crashed workers.
* SIGTERM/SIGINT: stop claiming, let in-flight jobs finish, exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket

import structlog
from pmi_core import queue
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.models import CoreJob

from pmi_workers import registry
from pmi_workers.notify import notify_failure

log = structlog.get_logger("pmi_workers.worker")

_STALE_SWEEP_INTERVAL_SEC = 60.0


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _listen_for_jobs(wake: asyncio.Event) -> None:
    """Dedicated LISTEN connection; sets `wake` on every notify. Reconnects
    with backoff; permanent failure just means we degrade to polling."""
    import asyncpg

    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    backoff = 1.0
    while True:
        try:
            conn = await asyncpg.connect(dsn=dsn)
            try:
                await conn.add_listener(queue.NOTIFY_CHANNEL, lambda *_: wake.set())
                log.info("worker.listening", channel=queue.NOTIFY_CHANNEL)
                backoff = 1.0
                # Keep the connection alive; asyncpg surfaces a dropped
                # connection via the ping failing.
                while True:
                    await asyncio.sleep(30)
                    await conn.execute("SELECT 1")
            finally:
                with contextlib.suppress(Exception):
                    await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("worker.listen_failed", error=str(exc)[:200], retry_sec=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _heartbeat_loop(job_id: int) -> None:
    while True:
        await asyncio.sleep(settings.worker_heartbeat_sec)
        try:
            async with session_scope() as session:
                await queue.heartbeat(session, job_id)
        except Exception as exc:  # noqa: BLE001 - heartbeat must never kill the job
            log.warning("worker.heartbeat_failed", job_id=job_id, error=str(exc)[:200])


async def _execute(job: CoreJob) -> None:
    """Run one claimed job to completion (success, retry-queued, or failed)."""
    hb = asyncio.create_task(_heartbeat_loop(job.id))
    log.info(
        "worker.job_start",
        job_id=job.id,
        name=job.name,
        attempt=job.attempts,
        args=job.args,
    )
    try:
        try:
            fn = registry.get(job.name)
        except KeyError:
            raise RuntimeError(
                f"job '{job.name}' not registered (available: {registry.all_names()})"
            ) from None
        result = await fn(**(job.args or {}))
        async with session_scope() as session:
            await queue.complete(session, job.id, result if isinstance(result, dict) else None)
        log.info("worker.job_done", job_id=job.id, name=job.name)
    except asyncio.CancelledError:
        # Shutdown mid-job: leave the row 'running'; the stale sweep of the
        # next worker generation re-queues it.
        raise
    except Exception as exc:  # noqa: BLE001 - job errors are data, not crashes
        error = f"{type(exc).__name__}: {exc}"
        log.exception("worker.job_error", job_id=job.id, name=job.name, error=error[:300])
        await notify_failure(job_name=job.name, exc=exc, worker_id=_worker_id(), args=job.args)
        retryable = not isinstance(exc, queue.PermanentJobError)
        async with session_scope() as session:
            await queue.fail(session, job, error, retryable=retryable)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb


async def run_worker(queues: list[str] | None = None) -> None:
    worker_id = _worker_id()
    log.info(
        "worker.start",
        worker_id=worker_id,
        concurrency=settings.worker_concurrency,
        queues=queues or ["*"],
        jobs=registry.all_names(),
    )

    wake = asyncio.Event()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    listener = asyncio.create_task(_listen_for_jobs(wake))
    running: set[asyncio.Task] = set()
    last_sweep = 0.0

    try:
        while not stop.is_set():
            now = loop.time()
            if now - last_sweep >= _STALE_SWEEP_INTERVAL_SEC:
                last_sweep = now
                try:
                    async with session_scope() as session:
                        await queue.requeue_stale(session)
                except Exception as exc:  # noqa: BLE001
                    log.warning("worker.sweep_failed", error=str(exc)[:200])

            claimed = None
            if len(running) < settings.worker_concurrency:
                try:
                    async with session_scope() as session:
                        claimed = await queue.claim_next(
                            session, worker_id=worker_id, queues=queues
                        )
                except Exception as exc:  # noqa: BLE001 - DB blip: back off, retry
                    log.warning("worker.claim_failed", error=str(exc)[:200])
                    await asyncio.sleep(settings.worker_poll_interval_sec)
                    continue

            if claimed is not None:
                task = asyncio.create_task(_execute(claimed))
                running.add(task)

                def _on_done(t: asyncio.Task) -> None:
                    running.discard(t)
                    wake.set()  # a slot freed up — claim again without waiting

                task.add_done_callback(_on_done)
                continue  # immediately try to claim the next one

            # Nothing claimable (or at concurrency) — sleep until notify/poll.
            wake.clear()
            wait_stop = asyncio.create_task(stop.wait())
            wait_wake = asyncio.create_task(wake.wait())
            done, pending = await asyncio.wait(
                {wait_stop, wait_wake},
                timeout=settings.worker_poll_interval_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        if running:
            log.info("worker.draining", in_flight=len(running))
            await asyncio.gather(*running, return_exceptions=True)
        log.info("worker.stopped", worker_id=worker_id)
