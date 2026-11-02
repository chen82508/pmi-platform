"""Venue taxonomy — the single source of truth for *money-type*.

Why this exists
---------------
A market's venue determines whether its traded "volume" is denominated in real
money or play money. That distinction is **orthogonal to relevance** (a Manifold
war market is genuinely about war) but it is *not* orthogonal to liquidity
weighting: the aggregator's quantile ladder ranks markets by orderbook depth /
volume, and play-money volume is fake currency that would corrupt the ladder.

So play-money markets are allowed to be index *members* (their price is still a
probability signal) but must be kept out of the liquidity quantile sample. The
`core_markets.is_play_money` column (a Postgres STORED generated column derived
from this set — see alembic 0010) is what the weighter reads; this module is the
one place the membership of that set is declared, and `play_money_sql_predicate`
keeps the migration's generation expression in lock-step with the constant.

Venue, not per-market: every market on these venues is play-money, so a
venue-keyed set is both correct and maintenance-free (no per-row tagging, no
backfill). Add a venue here + a migration if a new play-money source is ingested.
"""

from __future__ import annotations

# Play-money prediction markets. Their `volume` is denominated in site credits
# (Manifold "mana", Metaculus has no money at all — `volume` is engagement), so
# they must never enter the liquidity quantile ladder. Kept lowercase to match
# `core_markets.venue`.
PLAY_MONEY_VENUES: frozenset[str] = frozenset({"manifold", "metaculus"})


def is_play_money_venue(venue: str | None) -> bool:
    """True iff `venue` is a play-money exchange."""
    return venue in PLAY_MONEY_VENUES


def has_real_money(venues) -> bool:
    """True iff any venue in the iterable is real-money — the shared rule every
    vertical uses to decide whether a question/group/entity is shown: play-money
    enriches a question the real market also prices, but an all-play-money group
    (no real-money member) is dropped from display so a venue's user-generated
    noise can't create a phantom question. Weight is already 0 for play-money
    (money-type rule), so this only affects display, never a score."""
    return any(not is_play_money_venue(v) for v in venues)


def play_money_sql_predicate(column: str = "venue") -> str:
    """SQL boolean expression that is TRUE for play-money venues.

    Used by alembic 0010 to define the `is_play_money` generated column so the
    DB-side expression is generated from `PLAY_MONEY_VENUES` rather than being a
    hand-copied literal that can silently drift from this constant.
    """
    venues = ", ".join(f"'{v}'" for v in sorted(PLAY_MONEY_VENUES))
    return f"{column} IN ({venues})"
