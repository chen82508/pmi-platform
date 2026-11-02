"""ts_trades — per-trade fills from CLOB WS market channel + Polygon CTF Exchange.

Two writers populate this table:
* `pmi_ingest.streams.polymarket_ws` (CORR-4.1) — taps the CLOB WS
  `last_trade_price` / `market` channels for low-latency fills.
* `pmi_ingest.chain.polygon_indexer` (CORR-4.2) — back-fills from CTF
  Exchange `OrderFilled` event logs and enriches with maker / taker
  Polygon addresses + tx_hash.

Both paths share the same row shape; `source` distinguishes them. The
`(tx_hash, log_index)` unique constraint dedupes when both writers land
the same fill (WS arrives first → chain confirms later with addresses).

Future P1: TimescaleDB hypertable on `traded_at` (CORR-4.5). High row
volume — Polymarket sees ~10k trades/day across the universe.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class TsTrade(Base):
    __tablename__ = "ts_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(80), nullable=False)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # 'BUY' / 'SELL'

    maker_address: Mapped[str | None] = mapped_column(String(64))
    taker_address: Mapped[str | None] = mapped_column(String(64))
    tx_hash: Mapped[str | None] = mapped_column(String(80))
    log_index: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_external_id: Mapped[str | None] = mapped_column(String(128))

    raw: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_ts_trades__tx_hash_log_index"),
        Index("ix_ts_trades__market_time", "market_id", "traded_at"),
        Index("ix_ts_trades__taker", "taker_address", "traded_at"),
        Index("ix_ts_trades__maker", "maker_address", "traded_at"),
        Index("ix_ts_trades__trade_external_id", "trade_external_id"),
    )
