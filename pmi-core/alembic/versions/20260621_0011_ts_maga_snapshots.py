"""Add ts_maga_snapshots — pre-computed MAGA group heat cache.

The API reads from this table instead of recomputing from ts_price_snapshots
on every request, reducing response time from 30–80s to <10ms.

The worker job `maga-snapshot` (pmi-workers) refreshes every 10 minutes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ts_maga_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cycle_year", sa.Integer(), nullable=False),
        sa.Column("state_code", sa.String(2), nullable=False),
        sa.Column("state", sa.String(50), nullable=False),
        sa.Column("office", sa.String(20), nullable=False),
        sa.Column("district", sa.Integer(), nullable=True),
        sa.Column("heat", sa.Numeric(6, 2), nullable=False),
        sa.Column("n_markets", sa.Integer(), nullable=False),
        sa.Column("volume_24h", sa.Numeric(12, 2), nullable=False),
        sa.Column("base_question", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ts_maga_snapshots"),
    )
    # Latest group per (cycle_year, state, office, district) — used by API reads.
    op.create_index(
        "ix_ts_maga_snapshots__group_time",
        "ts_maga_snapshots",
        ["cycle_year", "state_code", "office", "district", sa.text("computed_at DESC")],
    )
    # Trend chart lookups: all groups for a state in a time window.
    op.create_index(
        "ix_ts_maga_snapshots__state_time",
        "ts_maga_snapshots",
        ["state_code", sa.text("computed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ts_maga_snapshots__state_time", table_name="ts_maga_snapshots")
    op.drop_index("ix_ts_maga_snapshots__group_time", table_name="ts_maga_snapshots")
    op.drop_table("ts_maga_snapshots")
