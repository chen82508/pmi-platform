"""core_market_watch / core_market_priority / core_market_id_watermark — priority ingestion.

Side tables for tiered ingest (P0 in-use, P1 new, P2 everything else). Pollers
read their in-use id lists from core_market_priority instead of filtering
core_markets, whose hot columns 0017 deliberately left unindexed. All three are
small (tens of thousands of rows) and written only by the refresh-market-priority
job plus poller activity upserts. See models/core_market_priority.py.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-11

NOTE (merge coordination): 0028 is reserved by the in-flight gemini PR (#107),
which 0029 already skips. If 0028 merges as a sibling of 0029, keep this chained
to whichever revision is head at merge time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_market_watch",
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("watcher", sa.Text(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_core_market_watch__expires", "core_market_watch", ["expires_at"])

    op.create_table(
        "core_market_priority",
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("rank", sa.REAL(), nullable=True),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_core_market_priority__venue_tier_rank",
        "core_market_priority",
        ["venue", "tier", sa.text("rank DESC")],
    )

    op.create_table(
        "core_market_id_watermark",
        sa.Column("taken_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("max_id", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("core_market_id_watermark")
    op.drop_table("core_market_priority")
    op.drop_table("core_market_watch")
