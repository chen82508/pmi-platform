"""Unit tests for election_cycle — cycle recovery from title / ticker / close.

Examples are real rows pulled from the live DB (kalshi SENATE*/KXHOUSERACE*,
forecastex MXX*/G0* tickers) so the parser is pinned to the actual formats, not
invented ones.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pmi_core.engine.election_cycle import election_cycle


def _dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# --------------------------------------------------------------------------
# kalshi — yearless title, cycle only in the ticker
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "external_id,title,expected",
    [
        # senate: SENATE<STATE>-<YY>-<SIDE>
        ("SENATECA-28-D", "Will Democratics win the Senate race in California?", 2028),
        ("SENATEAK-26-R", "Will Republicans win the Senate race in Alaska?", 2026),
        # senate special: an extra "-2" sub-segment must not be read as the year
        ("SENATEAL-28-2-D", "Will Democratics win the Senate race in Alabama?", 2028),
        # house: KXHOUSERACE-<STATE><DD>-<YY>-<SIDE>; "AZ04" is not a bare year
        ("KXHOUSERACE-AZ04-26-D", "Will Democratic win the House race for AZ-04?", 2026),
        ("KXHOUSERACE-CA13-26-R", "Will Republican win the House race for CA-13?", 2026),
    ],
)
def test_kalshi_cycle_from_ticker(external_id, title, expected):
    assert election_cycle("kalshi", title, external_id, None) == expected


def test_kalshi_ticker_beats_missing_title_year():
    # The whole point: title carries no year, ticker does.
    assert election_cycle(
        "kalshi",
        "Will Republicans win the Senate race in New York?",
        "SENATENY-28-R",
        _dt(2029, 11, 7),
    ) == 2028


# --------------------------------------------------------------------------
# forecastex — embedded MMDDYY election date, plus an explicit title year
# --------------------------------------------------------------------------

def test_forecastex_title_year_wins():
    assert election_cycle(
        "forecastex",
        "Will Shenna Bellows win the Maine general election for Governor in 2026?",
        "MXXME_110326_SB",
        _dt(2026, 11, 23),
    ) == 2026


def test_forecastex_date_ticker_when_title_has_no_year():
    # MMDDYY = 11/03/26 → 2026, recovered from the ticker alone.
    assert election_cycle("forecastex", "Will Shenna Bellows win?", "MXXME_110326_SB", None) == 2026
    assert election_cycle("forecastex", None, "G02TX_110326_DC", None) == 2026


# --------------------------------------------------------------------------
# title year (polymarket / predictit) and closes_at last-resort
# --------------------------------------------------------------------------

def test_title_year():
    assert election_cycle(
        "polymarket", "Will the Republicans win the Texas gubernatorial race in 2026?", "0xabc", None
    ) == 2026


@pytest.mark.parametrize(
    "closes,expected",
    [
        (_dt(2026, 11, 3), 2026),   # forecastex-style: closes the election year
        (_dt(2027, 11, 3), 2026),   # kalshi-style: closes the year after (odd → -1)
        (_dt(2029, 11, 7), 2028),
    ],
)
def test_closes_at_fallback(closes, expected):
    assert election_cycle("kalshi", "yearless title", "NOYEAR", closes) == expected


# --------------------------------------------------------------------------
# fail-open: no signal → None (caller keeps the market)
# --------------------------------------------------------------------------

def test_no_signal_returns_none():
    assert election_cycle("manifold", "Who will control the Senate?", "userslug", None) is None


def test_out_of_window_year_ignored():
    # A stray in-title number outside the cycle window must not coin a cycle;
    # nothing else resolves → None.
    assert election_cycle("polymarket", "Bitcoin above 2000 dollars?", None, None) is None
    # 2020 is below the accepted window; no other signal → None.
    assert election_cycle("polymarket", "The 2020 election redux", None, None) is None


def test_odd_year_in_title_not_taken_but_close_folds():
    # An odd explicit year isn't an even cycle; falls through to closes_at fold.
    assert election_cycle("polymarket", "Special 2025 runoff", None, _dt(2026, 3, 1)) == 2026
