"""core_market_game_tag — canonical game / event identity per sports market.

Lets the sports snapshot group components by WHERE sport/league/class instead of
a keyword pool, and collapses a game/outright across venues (Kalshi / Polymarket
/ Gemini). One row per classifiable market; written by the `tag-games` job. See
models/core_market_game_tag.py.

Revision ID: 0029
Revises: 0027
Create Date: 2026-10-06

NOTE (merge coordination): chained to 0027 on the feat/sports-vertical branch.
Kaito's in-flight gemini PR (#107) reserves 0028; when that merges first, rebase
and re-point down_revision to "0028".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_market_game_tag",
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sport", sa.String(length=16), nullable=False),
        sa.Column("league", sa.String(length=32), nullable=True),
        sa.Column("market_class", sa.String(length=12), nullable=False),
        sa.Column("home_team", sa.String(length=64), nullable=True),
        sa.Column("away_team", sa.String(length=64), nullable=True),
        sa.Column("game_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event", sa.String(length=80), nullable=True),
        sa.Column("competitor", sa.String(length=80), nullable=True),
        sa.Column("market_type", sa.String(length=16), nullable=False),
        sa.Column("line_value", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("side", sa.String(length=64), nullable=True),
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
        "ix_core_market_game_tag__sport_league_class",
        "core_market_game_tag",
        ["sport", "league", "market_class"],
    )
    op.create_index(
        "ix_core_market_game_tag__sport_event",
        "core_market_game_tag",
        ["sport", "event"],
    )


def downgrade() -> None:
    op.drop_table("core_market_game_tag")
