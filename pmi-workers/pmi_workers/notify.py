"""Best-effort Slack alerts for job failures (pmi-notify integration).

Mirrors `pmi_ingest.health.notify_failure`: called at each execution path's
existing exception boundary, purely additive — retry/backoff and
terminal-failure bookkeeping in `pmi_core.queue` (or the cron runner's
`sys.exit(1)`) is unchanged either way. There are two call sites, one per
execution path (see `runner.py::_run` for cron-triggered one-shot jobs,
`worker.py::_execute` for the durable Postgres-queue loop), each covering
every registered job uniformly rather than requiring per-job wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from pmi_notify import build_failure_message, get_notifier

log = structlog.get_logger(__name__)

# Scopes the Slack alert's filtered traceback to this package's own source,
# same rationale as pmi_ingest.health._INGEST_ROOT.
_WORKERS_ROOT = str(Path(__file__).resolve().parent)

_MAX_ARGS_LEN = 500


def _summarize_args(args: dict | None) -> str:
    """Short, JSON-ish rendering of a job's args for the alert body.

    Truncated rather than omitted on overflow — knowing which market/index a
    failed job was working on is usually the first thing triage needs.
    """
    if not args:
        return "{}"
    try:
        text = json.dumps(args, default=str)
    except Exception:  # noqa: BLE001 - args must never block the alert
        text = str(args)
    if len(text) > _MAX_ARGS_LEN:
        return text[:_MAX_ARGS_LEN] + "...(truncated)"
    return text


async def notify_failure(
    *,
    job_name: str,
    exc: Exception,
    worker_id: str | None = None,
    args: dict | None = None,
) -> None:
    """Best-effort Slack alert for a job failure. Never raises.

    `worker_id` / `args` are only known on the durable queue path (a claimed
    `CoreJob`) — the cron `run-job` path passes just `job_name`/`exc`.
    """
    try:
        message = build_failure_message(job_name, exc, include_root=_WORKERS_ROOT)
        if worker_id:
            message.fields["Worker"] = worker_id
        message.fields["Args"] = _summarize_args(args)
        await get_notifier().send(message)
    except Exception:  # noqa: BLE001 - notification delivery must never break the worker
        log.warning("workers.notify_failed", job_name=job_name, exc_info=True)
