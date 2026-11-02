"""Add core_markets.url — canonical venue deep-link for contract drill-down.

The war/maga UIs link each contributing contract out to its source market. Most
venues already supply the URL in their API response (stored verbatim in
core_markets.raw->>'url' — manifold/forecastex/predictit 100%%); polymarket &
kalshi are derived from slug. Previously there was no dedicated column, so the
UI could only link polymarket. Nullable, additive — every existing index/UI is
unchanged until the value is populated.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("core_markets", sa.Column("url", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("core_markets", "url")
