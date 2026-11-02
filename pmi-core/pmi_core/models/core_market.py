"""core_markets — mutable identity of a tradeable market (Polymarket only at P0)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, Computed, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base
from pmi_core.venues import play_money_sql_predicate


class CoreMarket(Base):
    """A market on a venue. Identity = (venue, external_id)."""

    __tablename__ = "core_markets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    # Canonical source URL (venue deep-link). Captured at ingest — most
    # venues supply it in their API (raw->>url); poly/kalshi derived from slug.
    url: Mapped[str | None] = mapped_column(String(512))

    category: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list[str] | None] = mapped_column(JSON)

    # Money-type, derived from `venue` (see pmi_core.venues). A STORED generated
    # column — no poller sets it, it can never drift from the venue, and existing
    # rows are populated by the migration. The liquidity weighter reads it to keep
    # play-money volume out of the quantile ladder; play-money markets are still
    # full index members. NOT a membership filter — relevance decides membership.
    is_play_money: Mapped[bool] = mapped_column(
        Boolean,
        Computed(play_money_sql_predicate("venue"), persisted=True),
        nullable=False,
    )

    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(32))  # 'YES' / 'NO' / 'INVALID' / NULL

    # On-chain identity (Polymarket). NULL for non-Polymarket venues until each
    # poller learns to populate them. `condition_id` is the CTF condition hash;
    # the two `clob_*_token` columns are the ERC-1155 outcome token IDs needed
    # to subscribe to CLOB WS / fetch /book / filter Polygon Transfer events.
    condition_id: Mapped[str | None] = mapped_column(String(80))
    clob_yes_token: Mapped[str | None] = mapped_column(String(80))
    clob_no_token: Mapped[str | None] = mapped_column(String(80))

    # UMA dispute / settle state from on-chain, separate from `resolution`
    # (which mirrors the Polymarket Gamma display field). Surfaced by the
    # chain indexer + uma_resolver — see CORR-4.4. Values: 'UMA_PROPOSED',
    # 'UMA_DISPUTED', 'UMA_SETTLED_YES', 'UMA_SETTLED_NO', 'UMA_SETTLED_INVALID'.
    chain_resolution: Mapped[str | None] = mapped_column(String(32))
    chain_resolution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict | None] = mapped_column(JSON)  # vendor payload for forward compat

    # Latest price snapshot — kept in sync by the ts_price_snapshots trigger
    # (migration 0013). Avoids scanning 160M+ snapshot rows for current prices.
    last_price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    last_volume_24h: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        {"comment": "Mutable market identity. New venues do NOT change this schema."},
    )
