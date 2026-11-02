"""movers-snapshot near-expiry ladder filter (`_is_expiring_ladder`).

The 2026-09-14 incident this guards: every one of the landing page's 12 "top
movers" was a same-day S&P intraday ladder leg or a current-week BTC strike at
±99 pts — settlement convergence, not repricing. The filter must drop exactly
those (threshold-titled markets closing within LADDER_CLOSE_WINDOW) while
never touching event markets, however close to their deadline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmi_workers.jobs.movers_snapshot import (
    LADDER_CLOSE_WINDOW,
    QUOTE_MAX_AGE,
    _is_expiring_ladder,
    _is_wide_book,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
SOON = NOW + timedelta(hours=4)  # inside the window
FAR = NOW + LADDER_CLOSE_WINDOW + timedelta(days=1)  # outside the window

# The two title families from the incident: kalshi index points (no $ sign)
# and kalshi/gemini dollar strikes.
SP_INTRADAY = "Will the S&P 500 be above 7509.9999 on Sep 14, 2026 at 10am EDT? - 7,510 or above"
BTC_WEEKLY = "Bitcoin price on Sep 18, 2026? - $67,000 or above"
BTC_RANGE = "Bitcoin price on Sep 18, 2026? - 75,000 to 79,999.99"


def test_same_day_index_ladder_excluded():
    assert _is_expiring_ladder(SP_INTRADAY, SOON, NOW)


def test_dollar_strike_ladder_excluded():
    assert _is_expiring_ladder(BTC_WEEKLY, SOON, NOW)


def test_range_bin_ladder_excluded():
    assert _is_expiring_ladder(BTC_RANGE, SOON, NOW)


def test_already_closed_unresolved_ladder_excluded():
    assert _is_expiring_ladder(SP_INTRADAY, NOW - timedelta(hours=2), NOW)


def test_daily_up_or_down_binary_excluded():
    # Strikeless but equally mechanical: a same-day direction coin flip.
    assert _is_expiring_ladder(
        "S&P 500 (SPX) Opens Up or Down on September 14?", SOON, NOW
    )


def test_far_dated_ladder_kept():
    # A quarterly strike stays a real mover until its own final week.
    quarterly = "Will the S&P 500 be above 8500 on Dec 31, 2026? - 8,500 or above"
    assert not _is_expiring_ladder(quarterly, FAR, NOW)


def test_event_market_kept_even_at_deadline():
    # Non-ladder titles are never expiry-filtered: a deadline surge on a war
    # market is exactly the repricing the movers table exists to show.
    assert not _is_expiring_ladder(
        "Iran-Oman Hormuz Agreement by September 14?", SOON, NOW
    )
    assert not _is_expiring_ladder(
        "Will Iran target Jordan by October 31, 2026?", SOON, NOW
    )


def test_percent_threshold_is_not_a_ladder():
    # GDP-style thresholds quote small percentages — two digits with a decimal
    # must not read as a strike.
    assert not _is_expiring_ladder(
        "Will US GDP growth be above 2.5% in Q3 2026?", SOON, NOW
    )


def test_seat_count_race_is_not_a_ladder():
    # "or more/fewer" (seat counts) is deliberately outside the pattern.
    assert not _is_expiring_ladder(
        "Will Republicans win 52 or more Senate seats?", SOON, NOW
    )


def test_unknown_close_kept():
    # No closes_at → can't prove expiry mechanics → keep (fail-open).
    assert not _is_expiring_ladder(SP_INTRADAY, None, NOW)


def test_naive_closes_at_treated_as_utc():
    assert _is_expiring_ladder(SP_INTRADAY, SOON.replace(tzinfo=None), NOW)


# --------------------------------------------------------------------------
# 2026-10-26 — wide-book filter (`_is_wide_book`): a last-trade Δ on a thin
# book is one print, not a repricing.
# --------------------------------------------------------------------------

FRESH = NOW - timedelta(minutes=20)


def test_phantom_mover_books_excluded():
    # kalshi "BTC hits $70,000 before $100,000 – 70,000 first" (13 → 63 on 17 / 61)
    assert _is_wide_book(Decimal("0.17"), Decimal("0.61"), FRESH, NOW)
    # kalshi "S&P 500 above $8,000 before Feb" (+66 on 16 / 85)
    assert _is_wide_book(0.16, 0.85, FRESH, NOW)


def test_ordinary_books_kept():
    assert not _is_wide_book(0.44, 0.45, FRESH, NOW)  # polymarket, 1 pt
    assert not _is_wide_book(0.34, 0.41, FRESH, NOW)  # 7 pts, the widest real one measured


def test_missing_or_one_sided_quote_kept():
    assert not _is_wide_book(None, None, None, NOW)  # Manifold AMM / no book
    assert not _is_wide_book(0.2, None, FRESH, NOW)
    assert not _is_wide_book(None, 0.9, FRESH, NOW)


def test_stale_quote_kept():
    assert not _is_wide_book(0.17, 0.61, NOW - QUOTE_MAX_AGE - timedelta(minutes=1), NOW)


def test_naive_quote_time_treated_as_utc():
    assert _is_wide_book(0.17, 0.61, FRESH.replace(tzinfo=None), NOW)
