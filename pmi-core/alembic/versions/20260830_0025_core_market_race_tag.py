"""core_market_race_tag — canonical election-race identity per market.

Lets election indexes select components by WHERE office/cycle instead of the
keyword-pool + id-DESC cap (which truncated older venues once kalshi bloated the
pool). One row per classifiable market; written by the `tag-races` job. See
models/core_market_race_tag.py.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_market_race_tag",
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("office", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("district", sa.Integer(), nullable=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("party_side", sa.String(length=1), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("resolved_via", sa.String(length=24), nullable=False),
        sa.Column("classifier_version", sa.Integer(), nullable=False),
        sa.Column(
            "tagged_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_core_market_race_tag__office_cycle",
        "core_market_race_tag",
        ["office", "cycle"],
    )


def downgrade() -> None:
    op.drop_table("core_market_race_tag")
