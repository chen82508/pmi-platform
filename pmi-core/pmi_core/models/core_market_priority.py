"""Priority-ingestion side tables: market watches, the materialised priority set,
and the id watermark used as a cheap "listed recently" clock.

They live outside ``core_markets`` on purpose: 0017 keeps ``last_price_at`` /
``last_volume_24h`` unindexed so the last-price trigger's UPDATEs stay HOT, so
pollers must never filter ``core_markets`` per cycle. Pollers read
``core_market_priority`` (tens of thousands of rows) instead; the
``refresh-market-priority`` job rebuilds it with PK lookups only. See
market_priority.py and docs/reports/priority_ingestion_blueprint.md.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    REAL,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreMarketWatch(Base):
    """Input signal: "watcher X is using market M until expires_at"."""

    __tablename__ = "core_market_watch"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id", ondelete="CASCADE"), primary_key=True
    )
    # 'activity:<venue>' (traded in 24h, emitted by pollers) or 'board:<name>'
    # (rendered by a computed-vertical snapshot). The prefix picks the tier.
    watcher: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_core_market_watch__expires", "expires_at"),)


class CoreMarketPriority(Base):
    """Output: the materialised P0/P1 set pollers read. Absent = P2."""

    __tablename__ = "core_market_priority"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id", ondelete="CASCADE"), primary_key=True
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    # Denormalised so a poller never has to touch core_markets to get its ids.
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[int] = mapped_column(
        SmallInteger, nullable=False
    )  # see pmi_core.market_priority.Tier
    rank: Mapped[float | None] = mapped_column(REAL)  # within-tier order; higher first
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Serves priority_ids(): one venue, tiers in order, best rank first.
Index(
    "ix_core_market_priority__venue_tier_rank",
    CoreMarketPriority.venue,
    CoreMarketPriority.tier,
    CoreMarketPriority.rank.desc(),
)


class CoreMarketIdWatermark(Base):
    """``max(core_markets.id)`` sampled over time: ``id > watermark(now - 48h)``
    is "listed in the last 48h" as a PK range scan (created_at is unindexed)."""

    __tablename__ = "core_market_id_watermark"

    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    max_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
