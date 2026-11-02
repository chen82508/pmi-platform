"""Multi-index `score-all` job.

Pulls every `is_current=true` `CoreIndexDefinition` from the database and
runs one pipeline tick per index, sequentially. Sequential is intentional at
P0: pmi-core's pipeline opens its own `session_scope()` and the LLM stub is
deterministic, so there's no concurrency win to extract until M1 (real LLM
calls) lands. Parallelism is the P1 Arq job.
"""

from __future__ import annotations

import structlog
from pmi_core.db import session_scope
from pmi_core.models import CoreIndexDefinition
from pmi_core.queue import PermanentJobError
from sqlalchemy import select

from pmi_workers.jobs.score import score_index
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.score_all")

# Faults that re-running cannot fix: a definition row whose YAML is absent from
# the image (registered in `core_index_definitions`, file never committed) fails
# identically on every attempt. Retrying the batch for it re-scores every OTHER
# index too, so a single orphan definition multiplies each beat's cost by
# `max_attempts` and still ends failed.
_PERMANENT = (FileNotFoundError,)


async def _current_index_ids() -> list[str]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CoreIndexDefinition.index_id)
                    .where(CoreIndexDefinition.is_current.is_(True))
                    .order_by(CoreIndexDefinition.index_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@register("score-all", queue="batch")
async def run() -> None:
    """Score every current index in registry order."""
    index_ids = await _current_index_ids()
    if not index_ids:
        log.warning("score_all.no_indexes")
        return

    log.info("score_all.start", count=len(index_ids), indexes=index_ids)
    failures: list[tuple[str, str]] = []
    transient = 0
    for idx in index_ids:
        try:
            await score_index(idx)
        except Exception as exc:
            log.exception("score_all.index_failed", index_id=idx, error=str(exc))
            failures.append((idx, str(exc)))
            if not isinstance(exc, _PERMANENT):
                transient += 1

    log.info(
        "score_all.done",
        succeeded=len(index_ids) - len(failures),
        failed=len(failures),
        transient_failures=transient,
        failures=failures,
    )
    if failures:
        # Bubble up either way so the failure stays visible in core_jobs.error and
        # in supercronic's exit code — but only ask for a retry when at least one
        # failure could actually succeed next time.
        detail = f"{len(failures)}/{len(index_ids)} indexes failed: {failures}"
        if transient:
            raise RuntimeError(detail)
        raise PermanentJobError(detail)
