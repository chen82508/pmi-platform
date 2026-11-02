"""Write-path performance: drop dead/duplicate indexes, throttle last-price trigger.

Driven by pg_stat evidence on the EC2 box (2026-07-25, stats since 07-04):

1. ``ix_ts_price_snapshots__market_time`` (21 GB, idx_scan=0) duplicates
   ``uq_ts_price_snapshots__market_time`` from 0005 — same (market_id,
   snapshot_at) columns, the planner always picks the unique one. Every one
   of the ~25M snapshot inserts/day maintains both. Dropped.
2. ``ix_ts_orderbook_snapshots__token_time`` (22 GB, idx_scan=0) has no code
   reader — the CLOB pollers resolve token→market_id at write time and the
   only query path (engine/pipeline.py liquidity join) filters by market_id
   via ``ix_ts_orderbook_snapshots__market_time``. Dropped.
3. ``ix_core_markets__last_price_at`` (796 MB, idx_scan=380 in 20 days) is
   what forces every ``fn_sync_market_last_price`` UPDATE to be non-HOT: the
   trigger writes last_price_at, so with the column indexed each of the
   ~38M updates/day re-enters ALL core_markets indexes (incl. the 1 GB
   title-trgm GIN). Its one filter user (routes/wc.py, near-dead post-WC26)
   falls back to the trgm index / seq scan. Dropped, and fillfactor=90 set so
   pages keep slack for HOT chains.
4. ``fn_sync_market_last_price`` fired an unconditional UPDATE per snapshot
   insert → ~780M core_markets row versions in 20 days (28.7k autovacuums).
   Now it only writes when the price actually moved, or on a 10-minute
   heartbeat so last_volume_24h / last_price_at stay ≤10 min stale for
   flat-priced markets (same cadence as the */10 board snapshots).

5. Codifies ``idx_core_markets_title_trgm`` (gin, gin_trgm_ops) — it existed
   on the EC2 box only as an out-of-band manual index, so every other
   environment ran the keyword selector, /gdp ILIKE board, and the wc/senate
   complement regexes as ~1M-row seq scans (measured 683ms per /gdp/board on
   the laptop). IF NOT EXISTS → no-op on EC2, real index everywhere else.
   Deliberately NOT dropped on downgrade: the EC2 copy predates this
   migration and must survive a downgrade.

Also creates pg_stat_statements (the view is inert until the server runs with
shared_preload_libraries=pg_stat_statements — wired in docker-compose.yml).

Locks: index drops need a brief ACCESS EXCLUSIVE on hot insert tables; a
10s lock_timeout makes the migration fail cleanly (re-run later) instead of
queueing the ingest fleet behind a long-running query.

Downgrade restores the old trigger + indexes; note the two snapshot-table
index rebuilds take hours at production row counts.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

THROTTLED_FN = """
    CREATE OR REPLACE FUNCTION fn_sync_market_last_price()
    RETURNS TRIGGER LANGUAGE plpgsql AS $$
    BEGIN
        UPDATE core_markets
        SET last_price      = NEW.last_price,
            last_volume_24h = NEW.volume_24h,
            last_price_at   = NEW.snapshot_at
        WHERE id = NEW.market_id
          AND (last_price_at IS NULL OR NEW.snapshot_at >= last_price_at)
          AND (last_price IS DISTINCT FROM NEW.last_price
               OR last_price_at IS NULL
               OR NEW.snapshot_at - last_price_at >= interval '10 minutes');
        RETURN NULL;
    END;
    $$;
"""

# 0013 original: unconditional write on every newer snapshot.
UNTHROTTLED_FN = """
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
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '10s'")
    op.execute("DROP INDEX IF EXISTS ix_ts_price_snapshots__market_time")
    op.execute("DROP INDEX IF EXISTS ix_ts_orderbook_snapshots__token_time")
    op.execute("DROP INDEX IF EXISTS ix_core_markets__last_price_at")
    op.execute("ALTER TABLE core_markets SET (fillfactor = 90)")
    op.execute(THROTTLED_FN)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_core_markets_title_trgm "
        "ON core_markets USING gin (title gin_trgm_ops)"
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute(UNTHROTTLED_FN)
    op.execute("ALTER TABLE core_markets RESET (fillfactor)")
    op.create_index("ix_core_markets__last_price_at", "core_markets", ["last_price_at"])
    op.create_index(
        "ix_ts_orderbook_snapshots__token_time",
        "ts_orderbook_snapshots",
        ["token_id", "snapshot_at"],
    )
    op.create_index(
        "ix_ts_price_snapshots__market_time",
        "ts_price_snapshots",
        ["market_id", "snapshot_at"],
    )
