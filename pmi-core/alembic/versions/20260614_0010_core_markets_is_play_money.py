"""Add core_markets.is_play_money — a generated money-type column.

Venue is no longer a *membership* filter (relevance decides whether a market
belongs to an index — see the selector/IR change in the same change-set). But
play-money venues (Manifold, Metaculus) must still be kept out of the liquidity
quantile ladder, because their "volume" is fake currency that would corrupt the
depth-ranked weights. This column is the signal the aggregator reads for that.

It is a Postgres **STORED generated column** derived from `venue`, so:
  * no poller has to set it (zero ingest changes, can never drift from venue),
  * existing rows are populated atomically when the column is added,
  * the generation expression is built from `pmi_core.venues.PLAY_MONEY_VENUES`
    via `play_money_sql_predicate`, so the constant is the single source of truth.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from pmi_core.venues import play_money_sql_predicate

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    predicate = play_money_sql_predicate("venue")
    op.execute(
        "ALTER TABLE core_markets "
        f"ADD COLUMN is_play_money BOOLEAN NOT NULL GENERATED ALWAYS AS ({predicate}) STORED"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE core_markets DROP COLUMN is_play_money")
