"""core_market_game_tag — a sports market's canonical game / event identity.

What a sports market bets on — WHICH sport & league, WHICH two teams on WHICH
date (a head-to-head *game*), or WHICH title a competitor is chasing (an
*outright*: "win the 2026 World Series"), or a *prop* — is a property of the
market itself, not of any board that renders it. It never changes with price and
is shared across venues: Kalshi / Polymarket / Gemini each list "Dodgers win the
World Series" under their own wording, and this row is what collapses them to one
cross-venue reading.

Mirrors `core_market_race_tag` (elections), for the same reason: it lets the
sports snapshot group components with a plain ``WHERE sport=… AND league=…``
instead of a keyword pool, and it moves the hard part — resolving messy team
names ("Los Angeles D" / "Dodgers" / "Los Angeles Dodgers"), the league, and the
market shape — into a one-time LLM classification that is cached, not re-run per
request (see the ``tag-games`` job + prompts/factors/game-tag-extraction).

market_class drives which fields are populated:
  game     → home_team, away_team, game_date (+ market_type: moneyline|runline|total)
  outright → event, competitor            (market_type: series|matchup|mvp|other)
  prop     → event/competitor (+ free-form; display-only, never folded into odds)

line_value / side carry the spread or total line (e.g. runline -1.5, total 8.5)
so cross-venue aggregation can match the SAME line — unused by MLB v1 (it has no
run line / game totals in our data) but populated for NFL/soccer with no schema
change. Price stays live in ts_price_snapshots; aggregation reads the tag for
identity and the snapshot for the current price.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreMarketGameTag(Base):
    __tablename__ = "core_market_game_tag"

    # One canonical game/event identity per market.
    market_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("core_markets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sport: Mapped[str] = mapped_column(String(16), nullable=False)  # mlb|nfl|soccer|…
    # League within the sport ("MLB", "EPL", "NFL"). NULL when the sport is its own
    # single league; kept distinct so multi-league sports (soccer) can scope by it.
    league: Mapped[str | None] = mapped_column(String(32))
    market_class: Mapped[str] = mapped_column(String(12), nullable=False)  # game|outright|prop

    # --- game (head-to-head) ---
    home_team: Mapped[str | None] = mapped_column(String(64))
    away_team: Mapped[str | None] = mapped_column(String(64))
    game_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- outright / prop ---
    event: Mapped[str | None] = mapped_column(String(80))  # "2026 World Series", "NLCS MVP"
    competitor: Mapped[str | None] = mapped_column(String(80))  # team or player this leg tracks

    # moneyline|runline|total|series|matchup|mvp|prop|other
    market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    line_value: Mapped[float | None] = mapped_column(Numeric(6, 2))  # spread/total line
    side: Mapped[str | None] = mapped_column(String(64))  # home|away|over|under|yes|<competitor>

    # patterns | llm_fallback — provenance of the classification.
    resolved_via: Mapped[str] = mapped_column(String(24), nullable=False)
    # Bumped when the extraction prompt/grammar changes; a stale value flags a
    # market for re-tag (mirrors core_market_race_tag.classifier_version).
    classifier_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # List a league's markets (the snapshot's hot query).
        Index("ix_core_market_game_tag__sport_league_class", "sport", "league", "market_class"),
        # Group an outright's competitors across venues ("who wins the World Series").
        Index("ix_core_market_game_tag__sport_event", "sport", "event"),
    )
