"""core_market_race_tag — a market's canonical election-race identity.

The identity of an election market — WHICH office / state / district / cycle it
bets on and WHICH party the leg tracks — is a property of the market itself, not
of any index that asks about it. It never changes with price and is shared by
every election index (the MAGA leans and the balance-of-power seat counts, all
three chambers).

Storing it here lets the election indexes SELECT their component markets with a
plain ``WHERE office=… AND cycle=…`` instead of the keyword-pool + id-DESC cap
the generic selector uses — which bloated past the cap once kalshi joined and
silently truncated older venues (polymarket's US-senate legs went 78→3). One row
per classifiable market; noise (news-utterance, esports, foreign, procedural)
gets no row and is thereby excluded with no cap needed.

The row is the OUTPUT of the aggregator's own keyword-path classification
(engine/aggregator._extraction_inputs + engine/extraction.classify_entities —
election_cycle + exclusion + the per-office parsers + party resolution, all three
extraction tiers). It is recomputed by the ``tag-races`` job — on new markets and,
when the grammar improves, a full re-tag (``classifier_version`` bumps so a re-tag
is detectable).
Price stays live in ts_price_snapshots; scoring reads the tag for identity and
the snapshot for the current price.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreMarketRaceTag(Base):
    __tablename__ = "core_market_race_tag"

    # One canonical race identity per market (a leg belongs to exactly one race).
    market_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("core_markets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    office: Mapped[str] = mapped_column(String(16), nullable=False)  # senate|house|governor
    # Canonical Title-Case state NAME ("California") — the us_state / us_governor
    # entity key space; for house the entity key is f"{state}-{district}".
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    district: Mapped[int | None] = mapped_column(Integer)  # house only; NULL otherwise
    # NULL = election_cycle couldn't be determined (yearless title / no ticker year
    # / no closes_at). Kept (not dropped) so the selector can fail-open it into the
    # current cycle, mirroring the aggregator's keyword-path cycle gate.
    cycle: Mapped[int | None] = mapped_column(Integer)  # even election year, or NULL
    party_side: Mapped[str] = mapped_column(String(1), nullable=False)  # 'R' | 'D'
    # party_direct (patterns / llm_fallback — a directed P(R) reading) | candidate.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The extraction tier that resolved it (patterns | candidates | llm_fallback)
    # — provenance, and the tier the aggregator rebuilds the observation with.
    resolved_via: Mapped[str] = mapped_column(String(24), nullable=False)
    # Bumped when the grammar changes; a stale value flags a market for re-tag.
    classifier_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The election indexes' hot query: markets of an office in a cycle.
        Index("ix_core_market_race_tag__office_cycle", "office", "cycle"),
    )
