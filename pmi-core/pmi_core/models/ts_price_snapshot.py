"""ts_price_snapshots — time-series of last/bid/ask + volume per market.

Future P1: convert to TimescaleDB hypertable on `snapshot_at`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class TsPriceSnapshot(Base):
    __tablename__ = "ts_price_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    last_price: Mapped[float | None] = mapped_column(Numeric(10, 6))
    bid: Mapped[float | None] = mapped_column(Numeric(10, 6))
    ask: Mapped[float | None] = mapped_column(Numeric(10, 6))
    volume_24h: Mapped[float | None] = mapped_column(Numeric(20, 4))
    liquidity: Mapped[float | None] = mapped_column(Numeric(20, 4))
    # Raw venue-reported cumulative/lifetime counter, when the venue only
    # exposes a stock rather than a 24h flow (Kalshi volume_fp, ForecastEx
    # open_interest). Never read directly as volume_24h — pollers diff it
    # against the ~24h-ago reading (see pollers/_base.py::compute_volume_24h_delta).
    cumulative_volume: Mapped[float | None] = mapped_column(Numeric(20, 4))

    __table_args__ = (
        # (market_id, snapshot_at) lookups ride the unique constraint's index;
        # a separate plain Index on the same columns was dropped in 0017 (it
        # duplicated 21GB of btree on the hottest insert path, idx_scan=0).
        UniqueConstraint("market_id", "snapshot_at", name="uq_ts_price_snapshots__market_time"),
    )
