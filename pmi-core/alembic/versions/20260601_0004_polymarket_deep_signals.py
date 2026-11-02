"""Polymarket deep signal schema (CORR-4.1 ~ 4.4).

Adds the on-chain + orderbook + WS trade data path the platform owes per
CLAUDE.md §5. Everything here is additive — old `ts_price_snapshots` and
`core_markets` rows continue to work; the new tables are populated by the
new pollers/streams in `pmi_ingest/`.

What lands
----------
* `ts_orderbook_snapshots` — per-market depth snapshot from CLOB `/book`.
  Feeds CORR-3.4 liquidity weighting (replaces volume proxy).
* `ts_trades` — per-trade rows from Polymarket CLOB WS market channel and/or
  Polygon CTF Exchange `OrderFilled` event. High row count — flagged as a
  Timescale hypertable candidate in CORR-4.5.
* `core_traders` — wallet identity + cohort label (whale/mid/retail) computed
  from rolling volume. Populated by chain indexer.
* `audit_chain_events` — append-only log of every on-chain event the indexer
  consumed. (Idempotency key = `(tx_hash, log_index)`.)
* New columns on `core_markets`:
  - `condition_id` (UMA/CTF condition hash) — required to map a market to
    its ERC-1155 outcome token IDs.
  - `clob_yes_token` / `clob_no_token` — outcome token IDs needed by CLOB
    /book + WS subscribe + Polygon Transfer filter.
  - `chain_resolution` / `chain_resolution_at` — UMA dispute / settle state
    surfaced from on-chain, separate from `resolution` (which mirrors the
    Polymarket Gamma display field). See CORR-4.4.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── core_markets: chain / CLOB identity columns ────────────────────────
    op.add_column(
        "core_markets",
        sa.Column("condition_id", sa.String(80), nullable=True),
    )
    op.add_column(
        "core_markets",
        sa.Column("clob_yes_token", sa.String(80), nullable=True),
    )
    op.add_column(
        "core_markets",
        sa.Column("clob_no_token", sa.String(80), nullable=True),
    )
    op.add_column(
        "core_markets",
        sa.Column("chain_resolution", sa.String(32), nullable=True),
    )
    op.add_column(
        "core_markets",
        sa.Column(
            "chain_resolution_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_core_markets__condition_id",
        "core_markets",
        ["condition_id"],
        unique=False,
    )
    op.create_index(
        "ix_core_markets__clob_yes_token",
        "core_markets",
        ["clob_yes_token"],
        unique=False,
    )

    # ── ts_orderbook_snapshots ─────────────────────────────────────────────
    op.create_table(
        "ts_orderbook_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "market_id",
            sa.BigInteger,
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_id", sa.String(80), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mid", sa.Numeric(10, 6)),
        sa.Column("spread", sa.Numeric(10, 6)),
        sa.Column("best_bid", sa.Numeric(10, 6)),
        sa.Column("best_ask", sa.Numeric(10, 6)),
        sa.Column("bid_depth_1pct", sa.Numeric(20, 4)),
        sa.Column("ask_depth_1pct", sa.Numeric(20, 4)),
        sa.Column("bid_depth_5pct", sa.Numeric(20, 4)),
        sa.Column("ask_depth_5pct", sa.Numeric(20, 4)),
        sa.Column("bid_total", sa.Numeric(20, 4)),
        sa.Column("ask_total", sa.Numeric(20, 4)),
        # Truncated raw book — top N levels per side, kept for forensics
        # (re-derive any depth quantile retroactively).
        sa.Column("bids", postgresql.JSON()),
        sa.Column("asks", postgresql.JSON()),
        comment=(
            "Orderbook depth snapshot per market token. Feeds CORR-3.4 "
            "liquidity weighting; replaces volume_24h proxy."
        ),
    )
    op.create_index(
        "ix_ts_orderbook_snapshots__market_time",
        "ts_orderbook_snapshots",
        ["market_id", "snapshot_at"],
    )
    op.create_index(
        "ix_ts_orderbook_snapshots__token_time",
        "ts_orderbook_snapshots",
        ["token_id", "snapshot_at"],
    )

    # ── ts_trades ──────────────────────────────────────────────────────────
    op.create_table(
        "ts_trades",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "market_id",
            sa.BigInteger,
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_id", sa.String(80), nullable=False),
        sa.Column("traded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(10, 6), nullable=False),
        sa.Column("size", sa.Numeric(20, 4), nullable=False),
        sa.Column(
            "side", sa.String(8), nullable=False
        ),  # 'BUY' or 'SELL' from taker perspective
        sa.Column("maker_address", sa.String(64)),
        sa.Column("taker_address", sa.String(64)),
        sa.Column("tx_hash", sa.String(80)),
        sa.Column("log_index", sa.Integer),
        sa.Column("source", sa.String(32), nullable=False),  # 'ws' / 'chain'
        sa.Column("trade_external_id", sa.String(128)),  # CLOB trade hash
        sa.Column("raw", postgresql.JSON()),
        sa.UniqueConstraint(
            "tx_hash", "log_index", name="uq_ts_trades__tx_hash_log_index"
        ),
        comment=(
            "Per-trade fills. Source = 'ws' from CLOB WS market channel "
            "or 'chain' from CTF Exchange OrderFilled event. The unique "
            "constraint deduplicates the same fill when seen via both."
        ),
    )
    op.create_index(
        "ix_ts_trades__market_time",
        "ts_trades",
        ["market_id", "traded_at"],
    )
    op.create_index(
        "ix_ts_trades__taker", "ts_trades", ["taker_address", "traded_at"]
    )
    op.create_index(
        "ix_ts_trades__maker", "ts_trades", ["maker_address", "traded_at"]
    )
    op.create_index(
        "ix_ts_trades__trade_external_id",
        "ts_trades",
        ["trade_external_id"],
        unique=False,
    )

    # ── core_traders ───────────────────────────────────────────────────────
    op.create_table(
        "core_traders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("address", sa.String(64), nullable=False, unique=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("trade_count", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("total_volume_usd", sa.Numeric(20, 4)),
        # Cohort label, updated periodically by chain indexer rollup.
        # Values: 'whale' (≥ $100k 30d notional), 'mid' (≥ $1k), 'retail'
        # (< $1k), 'unknown' (no rollup yet). Bands are tunable per CLAUDE.md §5.
        sa.Column(
            "cohort",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("cohort_updated_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", postgresql.JSON()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment=(
            "Polygon wallet identity + rolling cohort label. Used by "
            "aggregator.trader_cohort weighting (whale-favored markets boost)."
        ),
    )
    op.create_index("ix_core_traders__cohort", "core_traders", ["cohort"])

    # ── audit_chain_events ─────────────────────────────────────────────────
    op.create_table(
        "audit_chain_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # 'ctf_fill', 'ctf_transfer', 'uma_propose', 'uma_dispute',
        # 'uma_settle', 'condition_prepared', 'condition_resolved'
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("contract_address", sa.String(64), nullable=False),
        sa.Column("block_number", sa.BigInteger, nullable=False),
        sa.Column("block_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tx_hash", sa.String(80), nullable=False),
        sa.Column("log_index", sa.Integer, nullable=False),
        sa.Column("data", postgresql.JSON(), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tx_hash", "log_index", name="uq_audit_chain_events__tx_log"
        ),
        comment=(
            "Append-only chain event log. (tx_hash, log_index) is the "
            "natural idempotency key — re-running the indexer over the "
            "same block range is a no-op."
        ),
    )
    op.create_index(
        "ix_audit_chain_events__kind_block",
        "audit_chain_events",
        ["event_kind", "block_number"],
    )
    op.create_index(
        "ix_audit_chain_events__block",
        "audit_chain_events",
        ["block_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_chain_events__block", table_name="audit_chain_events"
    )
    op.drop_index(
        "ix_audit_chain_events__kind_block", table_name="audit_chain_events"
    )
    op.drop_table("audit_chain_events")

    op.drop_index("ix_core_traders__cohort", table_name="core_traders")
    op.drop_table("core_traders")

    op.drop_index("ix_ts_trades__trade_external_id", table_name="ts_trades")
    op.drop_index("ix_ts_trades__maker", table_name="ts_trades")
    op.drop_index("ix_ts_trades__taker", table_name="ts_trades")
    op.drop_index("ix_ts_trades__market_time", table_name="ts_trades")
    op.drop_table("ts_trades")

    op.drop_index(
        "ix_ts_orderbook_snapshots__token_time",
        table_name="ts_orderbook_snapshots",
    )
    op.drop_index(
        "ix_ts_orderbook_snapshots__market_time",
        table_name="ts_orderbook_snapshots",
    )
    op.drop_table("ts_orderbook_snapshots")

    op.drop_index("ix_core_markets__clob_yes_token", table_name="core_markets")
    op.drop_index("ix_core_markets__condition_id", table_name="core_markets")
    op.drop_column("core_markets", "chain_resolution_at")
    op.drop_column("core_markets", "chain_resolution")
    op.drop_column("core_markets", "clob_no_token")
    op.drop_column("core_markets", "clob_yes_token")
    op.drop_column("core_markets", "condition_id")
