"""Single-index `score` job.

Wraps `pmi_core.engine.run_pipeline()` so supercronic / Render cron can
trigger one PMI tick without learning the Python module path. Mirrors the
legacy job executor's PMI-score job pattern (job-class +
logger + Slack notify), but slimmed to the P0 essentials.

Invocation:

    run-job score:<index_id>            # via supercronic
    pmi-workers score <index_id>        # via Click CLI (debug)

The colon-form is a convention: any registered name containing `:` is
treated as `prefix:arg`. See [`pmi_workers.runner`](../runner.py).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import structlog
from pmi_core.engine import run_pipeline

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.score")

# Default index used when supercronic / cron triggers `score` with no arg.
# Set via env so deployment can pin to whichever index the env cares about
# without code changes.
DEFAULT_INDEX_ID = os.environ.get("PMI_WORKERS_DEFAULT_INDEX", "war-index-2026")


async def score_index(index_id: str) -> dict:
    """Run one pipeline tick for `index_id`. Returns the pipeline summary dict."""
    started = datetime.now(UTC)
    log.info("score.start", index_id=index_id, as_of=started.isoformat())
    result = await run_pipeline(index_id=index_id)
    ended = datetime.now(UTC)
    log.info(
        "score.done",
        index_id=index_id,
        score=result.get("score"),
        component_count=result.get("component_count"),
        markets_in=result.get("markets_in"),
        evaluations_written=result.get("evaluations_written"),
        cache_hits=result.get("cache_hits"),
        llm_calls=result.get("llm_calls"),
        cost_usd=result.get("cost_usd"),
        duration_s=(ended - started).total_seconds(),
    )
    return result


@register("score")
async def run_default(index_id: str | None = None) -> dict:
    """Score `index_id` (queue jobs pass it via args), else the env default."""
    return await score_index(index_id or DEFAULT_INDEX_ID)


@register("score:war-index-2026")
async def run_war_index() -> None:
    """Convenience alias matching the only baseline index at P0."""
    await score_index("war-index-2026")
