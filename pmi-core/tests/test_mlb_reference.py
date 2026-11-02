"""Pure-logic tests for the Kalshi MLB game-ticker parser."""
from pmi_core.engine.mlb_reference import parse_game_ticker


def test_three_char_pair_away_first():
    g = parse_game_ticker("KXMLBGAME-26OCT152000MILLAD-MIL")
    assert g is not None
    away, home, date = g
    # Kalshi orders the matchup away-first: MIL @ LAD.
    assert (away, home) == ("Milwaukee Brewers", "Los Angeles Dodgers")
    assert (date.year, date.month, date.day) == (2026, 10, 15)


def test_two_char_code_splits():
    # 'TBCLE' must split TB + CLE, not a bogus 3-char prefix.
    away, home, _ = parse_game_ticker("KXMLBGAME-26OCT151800TBCLE-CLE")
    assert (away, home) == ("Tampa Bay Rays", "Cleveland Guardians")


def test_nfl_ticker_is_not_mlb():
    # No HHMM block → the MLB regex must reject the NFL ticker shape.
    assert parse_game_ticker("KXNFLGAME-26OCT12BUFLAR") is None


def test_non_game_ticker_is_none():
    assert parse_game_ticker("KXMLBWINS-MIL-26-T80") is None
    assert parse_game_ticker(None) is None
