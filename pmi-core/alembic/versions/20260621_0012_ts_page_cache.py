"""Add ts_page_cache — pre-rendered API response blobs (JSONB, append-only).

One row per (cache_key, computed_at). API reads use:
  SELECT payload FROM ts_page_cache WHERE cache_key=? ORDER BY computed_at DESC LIMIT 1

Replaces direct ts_maga_snapshots reads in the API layer; ts_maga_snapshots is
retained as the historical time-series store for trend computation.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ts_page_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cache_key", sa.String(200), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ts_page_cache"),
    )
    op.create_index(
        "ix_ts_page_cache__key_time",
        "ts_page_cache",
        ["cache_key", sa.text("computed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ts_page_cache__key_time", table_name="ts_page_cache")
    op.drop_table("ts_page_cache")
