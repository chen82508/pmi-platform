"""page-cache-retention job — prune ts_page_cache (append-only, latest-wins).

The snapshot jobs append one row per run and nothing ever deletes, so the
table grew without bound (~118 MB in its first 10 days). Two access patterns
imply two retention rules:

* **Trend-source keys** (``war:{cycle}``, ``maga:{cycle}``, ``maga-trend:{cycle}``, ``climate-hub``,
  ``climate-board:*``, ``climate-question:*``, ``gdp-board:*``, ``gdp-period:*``,
  ``esports-match:*``, ``markets-trend:*`` — see ``TREND_KEY_PATTERN``) — their
  history IS the trend store, but the trend endpoints fold to one point per day
  and cap the window at 90 days. Rows older than ``trend_days`` are thinned to
  the day's last row; rows inside the window are kept whole (read_latest, the
  as_of reads and the daily fold all stay exact).
* **Everything else** (``war-country:*``, ``maga-state:*``, ``war-questions:*``,
  ``senate-board:*``, …) — only ever read via "latest row wins"; no endpoint
  reads their history. Keep the newest ``keep_latest`` rows per key as a
  debugging cushion, drop the rest.

``dry_run=true`` reports what would be deleted without deleting — run
``pmi-workers run-job page-cache-retention '{"dry_run": true}'`` first on a
fresh deploy. Deletes are plain DELETEs; autovacuum reclaims the space.
"""

from __future__ import annotations

import structlog
from pmi_core.db import session_scope
from sqlalchemy import text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.page_cache_retention")

# Trend-source keys: their ts_page_cache history IS the series store behind a
# trend/`*_series` endpoint, so it must be preserved (thinned to one row/day
# past `trend_days`), NOT pruned to the newest `keep_latest`. Site v2 widened
# this beyond war/maga to the climate, GDP, esports-match and markets anchor
# trend stores (D3), and 2026-09-14 to the seat boards (senate-board:*) — their
# history feeds the boards' control series AND the Seat pages' per-race P(R)
# trends (prob_by_state / prob_by_district paths), which keep_latest=24 had
# silently capped at ~24 hourly points. senate-board-trend:* (2026-10-20) is
# the slim copy of that same history the readers now prefer. This is
# CALENDAR-GATED — history only
# accrues forward, so the pattern is widened now even for pages that ship
# later, or those series start empty. Deliberately excluded (latest-render
# only, no history reader): markets-board:*, esports-board,
# war-country/region/questions:*, maga-state:*, landing:* — each keeps only
# the newest `keep_latest` rows.
TREND_KEY_PATTERN = (
    r"^(war|war-trend|maga|maga-trend):[0-9]+$"
    r"|^climate-hub(-trend)?$"
    r"|^(climate-board|climate-board-trend|climate-question|climate-question-trend"
    r"|gdp-board|gdp-board-trend|gdp-period|gdp-period-anchor"
    r"|esports-match|esports-match-trend|markets-trend|markets-anchor"
    r"|senate-board|senate-board-trend):"
)

# Latest-only keys: everything past the newest :keep_latest rows per key.
_LATEST_ONLY_CANDIDATES = """
    SELECT id FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY cache_key
                   ORDER BY computed_at DESC, id DESC
               ) AS rn
        FROM ts_page_cache
        WHERE cache_key !~ :trend_pattern
    ) ranked
    WHERE rn > :keep_latest
"""

# Trend keys: rows older than :trend_days that are not their day's last row.
_TREND_THIN_CANDIDATES = """
    SELECT id FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY cache_key, date(computed_at)
                   ORDER BY computed_at DESC, id DESC
               ) AS rn
        FROM ts_page_cache
        WHERE cache_key ~ :trend_pattern
          AND computed_at < now() - make_interval(days => :trend_days)
    ) ranked
    WHERE rn > 1
"""


async def _apply(session, candidates_sql: str, params: dict, dry_run: bool) -> int:
    if dry_run:
        return (
            await session.execute(text(f"SELECT count(*) FROM ({candidates_sql}) c"), params)
        ).scalar_one()
    result = await session.execute(
        text(f"DELETE FROM ts_page_cache t USING ({candidates_sql}) c WHERE t.id = c.id"),
        params,
    )
    return result.rowcount or 0


@register("page-cache-retention", queue="batch")
async def run(dry_run: bool = False, keep_latest: int = 24, trend_days: int = 90) -> dict:
    """Prune ts_page_cache; returns per-rule (would-be) delete counts."""
    async with session_scope() as session:
        latest_only = await _apply(
            session,
            _LATEST_ONLY_CANDIDATES,
            {"trend_pattern": TREND_KEY_PATTERN, "keep_latest": keep_latest},
            dry_run,
        )
        trend_thinned = await _apply(
            session,
            _TREND_THIN_CANDIDATES,
            {"trend_pattern": TREND_KEY_PATTERN, "trend_days": trend_days},
            dry_run,
        )
        if not dry_run:
            await session.commit()

    log.info(
        "page_cache_retention.done",
        dry_run=dry_run,
        latest_only_deleted=latest_only,
        trend_thinned_deleted=trend_thinned,
        keep_latest=keep_latest,
        trend_days=trend_days,
    )
    return {
        "dry_run": dry_run,
        "latest_only_deleted": latest_only,
        "trend_thinned_deleted": trend_thinned,
    }
