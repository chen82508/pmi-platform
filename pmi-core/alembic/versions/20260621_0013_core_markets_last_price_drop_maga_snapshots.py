"""Add core_markets.last_price; trigger to keep it in sync; drop ts_maga_snapshots.

core_markets gains three columns:
  last_price     — latest YES probability, 0–1
  last_volume_24h — 24h traded volume at last snapshot
  last_price_at  — timestamp of the snapshot that set these values

A AFTER INSERT trigger on ts_price_snapshots keeps them current automatically,
so no ingest poller code needs to change. Only updates when the new snapshot is
newer than the stored one (idempotent on out-of-order inserts).

ts_maga_snapshots is dropped: the MAGA worker now reads last_price from
core_markets (current state) and ts_price_snapshots with market_id filter
(trend history), then writes the full result to ts_page_cache.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. Add last_price columns to core_markets ────────────────────────────
    op.add_column("core_markets", sa.Column("last_price", sa.Numeric(10, 6), nullable=True))
    op.add_column("core_markets", sa.Column("last_volume_24h", sa.Numeric(14, 4), nullable=True))
    op.add_column("core_markets", sa.Column("last_price_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_core_markets__last_price_at", "core_markets", ["last_price_at"])

    # ── 2. PostgreSQL trigger: auto-update last_price on snapshot INSERT ──────
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_sync_market_last_price()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE core_markets
            SET last_price      = NEW.last_price,
                last_volume_24h = NEW.volume_24h,
                last_price_at   = NEW.snapshot_at
            WHERE id = NEW.market_id
              AND (last_price_at IS NULL OR NEW.snapshot_at >= last_price_at);
            RETURN NULL;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_sync_market_last_price
        AFTER INSERT ON ts_price_snapshots
        FOR EACH ROW EXECUTE FUNCTION fn_sync_market_last_price();
    """)

    # ── 3. Backfill: populate last_price for all markets from latest snapshot ─
    # Restricted to markets that have at least one snapshot. Runs once; the
    # trigger keeps it current from here on. May take ~30s on large tables.
    op.execute("""
        UPDATE core_markets m
        SET last_price      = s.last_price,
            last_volume_24h = s.volume_24h,
            last_price_at   = s.snapshot_at
        FROM (
            SELECT DISTINCT ON (market_id)
                market_id, last_price, volume_24h, snapshot_at
            FROM ts_price_snapshots
            ORDER BY market_id, snapshot_at DESC
        ) s
        WHERE m.id = s.market_id
    """)

    # ── 4. Drop ts_maga_snapshots (replaced by ts_page_cache JSONB) ──────────
    op.drop_index("ix_ts_maga_snapshots__state_time", table_name="ts_maga_snapshots")
    op.drop_index("ix_ts_maga_snapshots__group_time", table_name="ts_maga_snapshots")
    op.drop_table("ts_maga_snapshots")


def downgrade() -> None:
    # Recreate ts_maga_snapshots (empty — data is gone).
    op.create_table(
        "ts_maga_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("cycle_year", sa.Integer(), nullable=False),
        sa.Column("state_code", sa.String(2), nullable=False),
        sa.Column("state", sa.String(50), nullable=False),
        sa.Column("office", sa.String(20), nullable=False),
        sa.Column("district", sa.Integer(), nullable=True),
        sa.Column("heat", sa.Numeric(6, 2), nullable=False),
        sa.Column("n_markets", sa.Integer(), nullable=False),
        sa.Column("volume_24h", sa.Numeric(12, 2), nullable=False),
        sa.Column("base_question", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("DROP TRIGGER IF EXISTS trg_sync_market_last_price ON ts_price_snapshots")
    op.execute("DROP FUNCTION IF EXISTS fn_sync_market_last_price()")
    op.drop_index("ix_core_markets__last_price_at", table_name="core_markets")
    op.drop_column("core_markets", "last_price_at")
    op.drop_column("core_markets", "last_volume_24h")
    op.drop_column("core_markets", "last_price")
