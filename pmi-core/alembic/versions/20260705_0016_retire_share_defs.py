"""Retire us-{senate,house}-2026-republican-share (SCD2 close-out).

Data-only migration: closes the current rows of the two share defs
(is_current=false, effective_to=now()) so score-all stops scoring them and
/indexes stops listing them. Their YAMLs moved to index_defs/deprecated/
(see the README there for the full rationale): the published number was not
a share — the generic 50+raw×50 mapping can't go below 50 for an unsigned
index — and the component set was noisy (no gate factor, no D-side polarity
flip). Share is instead derived from the seats indexes:
E[R seats] / chamber size × 100.

History is untouched — ts_index_scores rows and the SCD2 definition history
stay queryable. Idempotent: re-running matches zero rows.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED_INDEX_IDS = (
    "us-senate-2026-republican-share",
    "us-house-2026-republican-share",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE core_index_definitions"
            " SET is_current = false, effective_to = now()"
            " WHERE index_id IN :ids AND is_current"
        ).bindparams(sa.bindparam("ids", RETIRED_INDEX_IDS, expanding=True))
    )


def downgrade() -> None:
    # Intentionally no-op: reopening is_current would need the original
    # effective_to values, and re-registering is a `pmi-core seed` away
    # (after moving the YAMLs back out of deprecated/).
    pass
