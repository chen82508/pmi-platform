"""core_market_esports_tag — precomputed esports match-winner classification.

The esports / LoL-Worlds folds discovered their winner markets by regex-scanning
titles + reading raw JSON across the whole live set every snapshot (a title regex
no index can serve → minutes under load). This table lets a tagger classify winner
markets once (same grammar the fold used) so the fold selects by an indexed game,
like every other vertical. Additive: new table + index only; nothing else changes.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_market_esports_tag",
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("resolved_via", sa.String(length=16), nullable=False),
        sa.Column("classifier_version", sa.Integer(), nullable=False),
        sa.Column(
            "tagged_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_core_market_esports_tag__game",
        "core_market_esports_tag",
        ["game"],
    )


def downgrade() -> None:
    op.drop_table("core_market_esports_tag")
