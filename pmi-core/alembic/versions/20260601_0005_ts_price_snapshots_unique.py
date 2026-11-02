"""Unique constraint on ts_price_snapshots(market_id, snapshot_at).

Needed by the Polymarket `/prices-history` backfill (CORR-3.10) so it can
`ON CONFLICT DO NOTHING` on re-runs. The forward-write polymarket_rest /
kalshi_rest pollers stamp `snapshot_at = datetime.now(UTC)` with
microsecond precision so this constraint will not collide with live data —
the only natural duplicate path is the backfill itself revisiting the
same 10-min bar.

Forward-compat note: when the table converts to a TimescaleDB hypertable
in CORR-4.5, the unique constraint must include the time dimension
(it already does — `snapshot_at`).

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_ts_price_snapshots__market_time",
        "ts_price_snapshots",
        ["market_id", "snapshot_at"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_ts_price_snapshots__market_time",
        "ts_price_snapshots",
        type_="unique",
    )
