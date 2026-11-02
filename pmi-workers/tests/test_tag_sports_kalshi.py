"""tag-sports-kalshi `_tag_row`: ticker → board tag, incl. NFL game totals, whose
"Full Game: over X points scored?" titles name no team (2026-10-29: 266 open
KXNFLTOTAL markets were ingested but never tagged)."""

from __future__ import annotations

from pmi_workers.jobs.tag_sports_kalshi import _tag_row


def test_nfl_total_from_ticker_and_title():
    r = _tag_row("KXNFLTOTAL-26NOV02CHISEA-47", "Full Game: over 46.5 points scored?")
    assert (r["sport"], r["mt"], r["line"], r["side"]) == ("nfl", "total", 46.5, "over")
    assert (r["away"], r["home"]) == ("Chicago Bears", "Seattle Seahawks")


def test_nfl_spread_line_is_negative_for_subject():
    r = _tag_row("KXNFLSPREAD-26OCT29CARGB-GB3", "Green Bay wins by over 3.5 Points?")
    assert (r["mt"], r["line"], r["side"]) == ("spread", -3.5, "Green Bay Packers")


def test_nfl_moneyline_side_from_trailing_code():
    r = _tag_row("KXNFLGAME-26OCT29CARGB-CAR", "Carolina wins")
    assert (r["mt"], r["side"], r["line"]) == ("moneyline", "Carolina Panthers", None)


def test_unowned_series_and_bad_tickers_skip():
    assert _tag_row("KXNFLTEAMTOTAL-26NOV02CHISEA-SEA24", "Seattle: over 23.5 points scored?") is None
    assert _tag_row("KXNCAAFTOTAL-26OCT31UKTENN-50", "Full Game: over 49.5 points scored?") is None
    assert _tag_row(None, "x") is None
