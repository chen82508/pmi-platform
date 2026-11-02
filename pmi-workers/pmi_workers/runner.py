"""CLI entry point used by supercronic: `run-job <job_name>`.

Ported from `micah-job-executor/app/runner.py`. The shape is identical so a
supercronic crontab from the legacy stack drops in unchanged; the only
difference is the registered job functions take no `db` argument (see
[`pmi_workers.registry`](registry.py) docstring).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog
from pmi_core.config import settings

# Importing the package triggers job registration via decorators.
from pmi_workers import registry
from pmi_workers.notify import notify_failure


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


log = structlog.get_logger("pmi_workers.runner")


async def _run(name: str) -> None:
    try:
        job = registry.get(name)
    except KeyError:
        names = ", ".join(registry.all_names()) or "(none)"
        print(f"error: job '{name}' not found. Registered: {names}", file=sys.stderr)
        sys.exit(2)

    log.info("job.start", name=name)
    try:
        await job()
    except Exception as exc:
        log.exception("job.failed", name=name, error=str(exc))
        await notify_failure(job_name=name, exc=exc)
        sys.exit(1)
    log.info("job.done", name=name)


def main() -> None:
    _setup_logging()
    from pmi_core.observability import init_sentry

    init_sentry("pmi-workers")
    if len(sys.argv) != 2:
        names = ", ".join(registry.all_names()) or "(none)"
        print(f"usage: run-job <job_name>\n  registered: {names}", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    main()
