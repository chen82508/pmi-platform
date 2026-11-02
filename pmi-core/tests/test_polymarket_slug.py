"""Pure-logic tests for the Polymarket MLB game-slug parser."""
from pmi_core.engine.polymarket_slug import parse_mlb_game


def test_moneyline_away_first_and_side():
    g = parse_mlb_game("mlb-mil-lad-2026-10-15")
    assert g is not None
    assert (g.away, g.home) == ("Milwaukee Brewers", "Los Angeles Dodgers")
    assert (g.game_date.year, g.game_date.month, g.game_date.day) == (2026, 10, 15)
    assert g.market_type == "moneyline"
    assert g.side == "Milwaukee Brewers"  # price is P(outcomes[0]) = P(away)


def test_full_game_total_line():
    g = parse_mlb_game("mlb-tb-cle-2026-10-15-total-7pt5")
    assert g.market_type == "total" and g.line == 7.5
    assert (g.away, g.home) == ("Tampa Bay Rays", "Cleveland Guardians")


def test_non_game_suffixes_rejected():
    # team-total / first-5 / props / run line are not full-game headline markets.
    for s in (
        "mlb-tb-cle-2026-10-15-team-total-tb-4pt5",
        "mlb-tb-cle-2026-10-15-f5-total-2pt5",
        "mlb-tb-cle-2026-10-15-nrfi",
        "mlb-tb-cle-2026-10-15-spread-home-2pt5",
        "mlb-cle-tb-2026-10-19-inning-3-winner-draw",
    ):
        assert parse_mlb_game(s) is None


def test_non_mlb_slug_rejected():
    # The LLM mis-tags these as sport=mlb; the slug prefix is the real filter.
    for s in ("cpbl-chi-wei-2026-10-20", "npb-yok-yom-2026-06-27",
              "cfb-cin-wvir-2026-10-17-1h-team-total-cin-11pt5", None):
        assert parse_mlb_game(s) is None
