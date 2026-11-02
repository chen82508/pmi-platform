"""Hourly cron: recompute every current PMI.

Identical surface to `score-all` for now (P0 only has one index), but kept
separate so the cron contract — "what fires hourly" vs "what's the action"
— stays decoupled. Add Polymarket WS-triggered re-evals here once pmi-ingest
WS ships (P1).
"""

from __future__ import annotations

import structlog

from pmi_workers.jobs.score_all import run as score_all_run
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.hourly")


@register("hourly", queue="batch")
async def run() -> None:
    """Hourly tick: score every current index."""
    log.info("hourly.start")
    await score_all_run()
    log.info("hourly.done")
