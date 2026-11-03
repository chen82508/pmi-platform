"""Make ts_index_scores.score nullable so the pipeline can persist
"no score this tick" without leaving a stale value visible.

Backports a fix from the legacy Micah stack, where
the bug was countries dropping below ``MIN_CONTRACTS`` keeping the last
computed PMI (Nigeria stuck at 76.5). On this platform the equivalent
trigger is ``len(collapsed) < ir.aggregation.min_components`` or zero
weighted relevancy across components — both now flow through as
``score=NULL`` rather than ``0.0``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "ts_index_scores",
        "score",
        existing_type=sa.Numeric(10, 4),
        nullable=True,
    )


def downgrade() -> None:
    # Backfill any NULLs introduced under the new contract before re-tightening
    # the column, otherwise the ALTER fails. 0.0 matches the pre-fix behaviour
    # so the rollback path mirrors what the aggregator used to write.
    op.execute("UPDATE ts_index_scores SET score = 0 WHERE score IS NULL")
    op.alter_column(
        "ts_index_scores",
        "score",
        existing_type=sa.Numeric(10, 4),
        nullable=False,
    )
