"""core_traders — Polygon wallet identity + cohort label.

Populated by `pmi_ingest.chain.polygon_indexer` rollup pass. The cohort
classification (whale / mid / retail) feeds CLAUDE.md §5 `trader_cohort`
weighting: a market dominated by whales gets a confidence boost in the
aggregator vs. one filled by retail only.

Cohort thresholds (defaults; tunable per index_def in future)
------------------------------------------------------------
* whale  — ≥ $100k notional in trailing 30d
* mid    — ≥ $1k
* retail — < $1k
* unknown — no rollup row yet (new wallet, indexer hasn't caught up)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreTrader(Base):
    __tablename__ = "core_traders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trade_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_volume_usd: Mapped[float | None] = mapped_column(Numeric(20, 4))

    cohort: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    cohort_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_core_traders__cohort", "cohort"),)
