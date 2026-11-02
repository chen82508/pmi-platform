"""ts_orderbook_snapshots — per-market orderbook depth from CLOB /book.

Feeds CORR-3.4 liquidity weighting (replaces volume_24h proxy). One row per
(market_id, token_id, snapshot_at). The `bids` / `asks` JSON columns keep the
top-N raw levels so any depth quantile can be re-derived retroactively.

Future P1: convert to TimescaleDB hypertable on `snapshot_at` (CORR-4.5).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class TsOrderbookSnapshot(Base):
    __tablename__ = "ts_orderbook_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(80), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    mid: Mapped[float | None] = mapped_column(Numeric(10, 6))
    spread: Mapped[float | None] = mapped_column(Numeric(10, 6))
    best_bid: Mapped[float | None] = mapped_column(Numeric(10, 6))
    best_ask: Mapped[float | None] = mapped_column(Numeric(10, 6))

    # Depth within N% of mid, in shares. Captures both the cumulative size and
    # the "is the book one-sided" question — aggregator can quotient bid/ask.
    bid_depth_1pct: Mapped[float | None] = mapped_column(Numeric(20, 4))
    ask_depth_1pct: Mapped[float | None] = mapped_column(Numeric(20, 4))
    bid_depth_5pct: Mapped[float | None] = mapped_column(Numeric(20, 4))
    ask_depth_5pct: Mapped[float | None] = mapped_column(Numeric(20, 4))
    bid_total: Mapped[float | None] = mapped_column(Numeric(20, 4))
    ask_total: Mapped[float | None] = mapped_column(Numeric(20, 4))

    bids: Mapped[list | None] = mapped_column(JSON)
    asks: Mapped[list | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_ts_orderbook_snapshots__market_time", "market_id", "snapshot_at"),
        # No token_id index: the CLOB pollers resolve token→market_id at write
        # time and every reader filters by market_id. The (token_id, snapshot_at)
        # index was dropped in 0017 (22GB, idx_scan=0 on prod).
    )
