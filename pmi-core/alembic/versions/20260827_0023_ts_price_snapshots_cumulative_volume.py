"""Add ts_price_snapshots.cumulative_volume — raw venue counter for 24h delta.

Kalshi (`volume_fp`) and ForecastEx (`open_interest`) only expose a
cumulative/stock counter, not a true 24h traded-volume flow. A prior fix
stopped writing those counters into `volume_24h` (they were corrupting
cross-venue liquidity weighting). This column lets the pollers persist the
raw counter alongside each snapshot so a real 24h delta can be computed by
diffing against the reading from ~24h ago (see
`pollers/_base.py::compute_volume_24h_delta` /
`fetch_cumulative_volume_24h_ago`). Nullable, additive — Polymarket/Manifold/
Metaculus (which already report a true 24h flow) never populate it.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ts_price_snapshots",
        sa.Column("cumulative_volume", sa.Numeric(20, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ts_price_snapshots", "cumulative_volume")
