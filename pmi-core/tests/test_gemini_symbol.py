"""Pure-logic tests for the Gemini instrumentSymbol parser."""
from pmi_core.engine.gemini_symbol import parse


def test_served_game_resolves_teams_and_date():
    g = parse("GEMI-MLB-2606302235-CHW-BAL-M-BAL")
    assert g is not None
    assert g.sport == "mlb"
    # CHW is Gemini's code for the White Sox (we store CWS); the alias resolves it.
    assert (g.away, g.home) == ("Chicago White Sox", "Baltimore Orioles")
    assert (g.game_date.year, g.game_date.month, g.game_date.day) == (2026, 6, 30)


def test_nfl_jacksonville_alias():
    g = parse("GEMI-NFL-2610180000-HOU-JAC-M-JAC")
    assert g.sport == "nfl"
    assert (g.away, g.home) == ("Houston Texans", "Jacksonville Jaguars")


def test_unserved_sport_is_other_with_no_teams():
    # A Pistons/Timberwolves game Gemini labels "Detroit vs Minnesota" — the token
    # keeps it off the MLB/NFL boards instead of letting the LLM guess it onto them.
    g = parse("GEMI-NBA-2610150000-DET-MIN-M-DET")
    assert g.sport == "other" and g.away is None and g.home is None


def test_futures_symbol_keeps_sport_without_a_game():
    g = parse("GEMI-NFLMVP2026-27-DST-T3")
    assert g.sport == "other" and g.home is None  # NFLMVP2026 != NFL token


def test_non_gemini_input_is_none():
    assert parse("KXNFLGAME-26OCT18HOUJAC-JAC") is None
    assert parse(None) is None
