"""Partial index for the Insights "Past issues" list (2026-10-22 statement timeouts).

`pmi_api.routes.insights._PAST_SQL` selects the latest row of every
`insight:issue:{iso_week}` key with `cache_key LIKE 'insight:issue:%'`. The
database collation is en_US.utf8, so the existing btree
`ix_ts_page_cache__key_time` cannot serve a LIKE prefix, and every /insights
page load seq-scanned all of ts_page_cache (~860k rows, 508 MB heap) to find
~40 rows. Warm that is 0.17 s; cold, on the I/O-bound data box, it ran 15–140 s
and pmi-api's 30 s statement timeout cancelled /insights and /insights/{week}
eight times on 2026-10-22.

The index predicate repeats the query's WHERE clause verbatim, which is what
lets the planner use a partial index; the query itself is unchanged, so its
results are unchanged. The index holds only insight-issue rows (≤7 per ISO week).

Built CONCURRENTLY inside an autocommit block so the apply never blocks the
snapshot jobs writing ts_page_cache. A failed concurrent build leaves an
INVALID index that `IF NOT EXISTS` would then skip — check
`pg_index.indisvalid` and DROP/rebuild by hand if a build was interrupted.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ix_ts_page_cache__insight_issue"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NAME} ON ts_page_cache"
            " (cache_key, computed_at DESC) WHERE cache_key LIKE 'insight:issue:%'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
