"""core_market_race_tag.cycle → nullable (cycle fail-open).

A market whose election_cycle can't be determined (yearless title, no ticker
year, no closes_at) is still a real race leg; the tag now keeps it with cycle
NULL so the selector can fail-open it into the current cycle — mirroring the
aggregator's keyword-path cycle gate (gate 1 keeps undeterminable-cycle legs).
Fail-closed (dropping them) silently lost kalshi's yearless senate legs.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "core_market_race_tag",
        "cycle",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Not reversible without dropping the fail-open rows; NULLs would violate the
    # old NOT NULL. Delete cycle-NULL rows first, then restore the constraint.
    op.execute("DELETE FROM core_market_race_tag WHERE cycle IS NULL")
    op.alter_column(
        "core_market_race_tag",
        "cycle",
        existing_type=sa.Integer(),
        nullable=False,
    )
