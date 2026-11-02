"""Deterministic structural votes for the BTC and S&P 500 gold sets.

These exist to break the circularity of a model-only protocol, so the cases
that matter most are the ABSTENTIONS: a structural rule that guesses
confidently on an ambiguous title is worse than no rule at all, because it
would silently ratify whatever the models said.
"""

from __future__ import annotations

import pytest

from pmi_core.eval.structural import (
    btc_direction,
    btc_major_move,
    btc_price_membership,
    sp500_direction,
    sp500_major_move,
    sp500_price_membership,
    structural_vote,
)


@pytest.mark.parametrize(
    "title",
    [
        "Bitcoin above $64,000 on June 24?",
        "Bitcoin price on Jun 23, 2026? - $55,900 or above",
        "Will Bitcoin's price exceed $63,500 this week?",
        "Bitcoin Up or Down - July 4, 3PM ET",
        "Bitcoin all time high by June 30, 2026?",
    ],
)
def test_price_markets_vote_yes(title: str) -> None:
    assert btc_price_membership(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Will Trump say Bitcoin during the address?",
        "Will MicroStrategy add BTC to its treasury in Q3?",
        "Will the SEC approve a spot Bitcoin ETF in 2026?",
        "Will Bitcoin hash rate set a record this month?",
    ],
)
def test_bitcoin_but_not_price_votes_no(title: str) -> None:
    assert btc_price_membership(title) is False


def test_non_bitcoin_market_is_a_confident_no() -> None:
    """The corpus_random stratum is almost entirely this case — the rule has to
    dispose of it without help, or it contributes nothing where it is needed most."""
    assert btc_price_membership("Will Ethereum close above $4,000 in December?") is False
    assert btc_price_membership("Will the Yankees win the AL East?") is False


def test_mixed_signal_titles_abstain_rather_than_guess() -> None:
    """Both an excluded class AND a price threshold — genuinely contested, so the
    structural voter must step back and let the models argue."""
    assert btc_price_membership("Will ETF approval push BTC above $80k by March?") is None


def test_empty_title_abstains() -> None:
    assert btc_price_membership(None) is None
    assert btc_price_membership("") is None


def test_major_move_has_no_structural_check() -> None:
    """Explicit abstain, so it is obvious this factor's gold rests on model
    consensus alone."""
    assert btc_major_move("Bitcoin above $64,000 on June 24?") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bitcoin above $64,000 on June 24?", 1.0),
        ("Bitcoin price on Jun 23? - $55,900 or above", 1.0),
        ("Will Bitcoin fall below $50,000 by Friday?", -1.0),
        ("Bitcoin between $62,000 and $64,000 on Dec 31?", None),
        ("Bitcoin price 75,000 to 79,999.99", None),
    ],
)
def test_direction_reads_title_polarity(title: str, expected: float | None) -> None:
    assert btc_direction(title) == expected


def test_structural_vote_normalises_to_floats() -> None:
    assert structural_vote("btc_price_relevance", "Bitcoin above $64,000?", None) == 1.0
    assert structural_vote("btc_price_relevance", "Will Trump say Bitcoin?", None) == 0.0
    assert structural_vote("btc_major_move", "Bitcoin above $64,000?", None) is None
    # A question with no registered voter simply has no structural check.
    assert structural_vote("unregistered_factor", "anything", None) is None


# ── S&P 500 ────────────────────────────────────────────────────────────────
# The one structural difference from BTC: kalshi writes S&P strikes as bare
# index points ("above 6845.5"), so these rules cannot lean on a `$` the way
# `btc_price_membership` leans on `extract_strike`.


@pytest.mark.parametrize(
    "title",
    [
        "Will the S&P 500 be above 6845.5 on Dec 31, 2026 at 4pm EST?",
        "Will the S&P 500 be below 4000 on Dec 31, 2026?",
        "S&P 500 (SPX) Up or Down on July 1?",
        "Will S&P 500 (SPY) hit (LOW) $720 in June?",
        "Will the S&P 500 set a new all-time high in 2026?",
    ],
)
def test_sp500_level_markets_vote_yes(title: str) -> None:
    assert sp500_price_membership(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Will the S&P 500 have the best performance of any major asset in 2026?",
        "Will Tesla be removed from the S&P 500 in 2026?",
        "Will the S&P 500's trailing P/E exceed 30 in 2026?",
    ],
)
def test_sp500_mentioned_but_not_the_level_votes_no(title: str) -> None:
    assert sp500_price_membership(title) is False


def test_sp500_non_index_market_is_a_confident_no() -> None:
    assert sp500_price_membership("Will the Nasdaq close above 25,000?") is False
    assert sp500_price_membership("Will the Yankees win the AL East?") is False


def test_sp500_macro_basket_with_a_level_abstains() -> None:
    """A recession bet that also names a level is genuinely contested."""
    assert (
        sp500_price_membership("Will a recession push the S&P 500 below 4000 in 2026?") is None
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Will the S&P 500 be above 6845.5 on Dec 31, 2026?", 1.0),
        ("Will the S&P 500 be below 4000 on Dec 31, 2026?", -1.0),
        # Polymarket's candle notation is the direction itself, and has to beat
        # the generic "hit" that would otherwise read as bullish.
        ("Will S&P 500 (SPY) hit (LOW) $720 in June?", -1.0),
        ("Will S&P 500 (SPX) hit $9,300 (HIGH) in December?", 1.0),
        # Range bins carry no direction.
        ("Will the S&P 500 be between 7400 and 7599.99 on Dec 31, 2026?", None),
    ],
)
def test_sp500_direction_reads_title_polarity(title: str, expected: float | None) -> None:
    assert sp500_direction(title) == expected


def test_sp500_major_move_has_no_structural_check() -> None:
    assert sp500_major_move("Will the S&P 500 be above 6845.5?") is None
