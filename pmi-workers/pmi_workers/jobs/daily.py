"""Daily cron: heavier maintenance.

P0: placeholder that just runs `score-all` (because nothing else is wired
yet). The shape mirrors `micah-job-executor/app/jobs/daily.py` so we can
add embedding regeneration / peer-group rebuild / Tier 3 re-evaluation as
they land without changing the supercronic crontab.
"""

from __future__ import annotations

import structlog

from pmi_workers.jobs.embed_markets import run as embed_markets_run
from pmi_workers.jobs.score_all import run as score_all_run
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.daily")


@register("daily", queue="long")
async def run() -> None:
    """Daily tick: embed new markets, then full re-score."""
    log.info("daily.start")
    # Embed before scoring so SemanticSelector / Tier 0 see markets ingested
    # since the last tick. Idempotent (sha-keyed), $0 on the Ollama default.
    await embed_markets_run()
    await score_all_run()
    # Future hooks (P1+):
    #   - rebuild peer-group cache (CLAUDE.md §5 trader cohort)
    #   - emit cost/coverage rollups to Slack
    log.info("daily.done")
