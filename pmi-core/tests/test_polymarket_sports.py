"""Deterministic Polymarket sports tagger — fixtures are live raw payloads (2026-10-27)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from pmi_core.engine.polymarket_sports import parse_polymarket_sports
from pmi_core.engine.sports_tag import NON_GAME_SENTINEL


def _m(slug: str | None, smt: str | None, title: str = "", venue: str = "polymarket", **raw):
    payload = {"slug": slug, "gameStartTime": "2026-11-07 23:15:00+00", **raw}
    if smt is not None:
        payload["sportsMarketType"] = smt
    return SimpleNamespace(id=7, venue=venue, title=title, raw=payload)


def test_cfb_is_not_nfl():
    # The LLM tagged this nfl / home "Tennessee Titans" — it's Kentucky @ Tennessee (CFB).
    r = parse_polymarket_sports(_m("cfb-uk-tenn-2026-11-07-spread-home-12pt5", "spreads",
                                   "Spread: Tennessee (-12.5)", line=-12.5))
    assert (r["sport"], r["league"], r["market_class"], r["market_type"]) == ("other", "cfb", "game", "spread")
    assert r["home_team"] is None and r["away_team"] is None
    assert r["line_value"] == -12.5
    assert r["game_date"] == datetime(2026, 11, 7, 23, 15, tzinfo=UTC)
    assert r["resolved_via"] == "slug"


def test_period_markets_are_props():
    r = parse_polymarket_sports(_m("cfb-ucla-minnst-2026-11-07-1q-total-7pt5", "q1_totals",
                                   "UCLA vs. Minnesota: 1Q O/U 7.5", line=7.5))
    assert (r["market_class"], r["market_type"], r["event"]) == ("prop", "total", "UCLA vs. Minnesota")


def test_nfl_spread_side_from_slug():
    r = parse_polymarket_sports(_m("nfl-tb-car-2026-10-25-spread-home-9pt5", "spreads",
                                   "Spread: Panthers (-9.5)", line=-9.5,
                                   outcomes='["Panthers", "Buccaneers"]'))
    assert (r["sport"], r["away_team"], r["home_team"]) == ("nfl", "Tampa Bay Buccaneers", "Carolina Panthers")
    assert (r["side"], r["line_value"], r["market_class"]) == ("Carolina Panthers", -9.5, "game")


def test_nfl_moneyline_side_is_outcome0():
    r = parse_polymarket_sports(_m("nfl-tb-car-2026-10-25", "moneyline", "Buccaneers vs. Panthers",
                                   outcomes=["Panthers", "Buccaneers"]))
    assert (r["market_type"], r["side"]) == ("moneyline", "Carolina Panthers")


def test_nfl_total_and_player_prop():
    t = parse_polymarket_sports(_m("nfl-cin-bal-2026-10-25-total-40pt5", "totals",
                                   "Bengals vs. Ravens: O/U 40.5", line=40.5))
    assert (t["market_type"], t["side"], t["line_value"]) == ("total", "over", 40.5)
    p = parse_polymarket_sports(_m("nfl-nyg-hou-2026-10-25-ryd-cj-stroud-29pt5", "rushing_yards",
                                   "C.J. Stroud: Rushing Yards O/U 29.5", line=29.5))
    assert (p["sport"], p["market_class"], p["market_type"], p["side"]) == ("nfl", "prop", "prop", None)


def test_nfl_rams_slug_code_la():
    # Polymarket slugs the Rams "la"; before the alias these fell to the LLM.
    r = parse_polymarket_sports(_m("nfl-lac-la-2026-11-01-total-46pt5", "totals",
                                   "Chargers vs. Rams: O/U 46.5", line=46.5))
    assert (r["away_team"], r["home_team"]) == ("Los Angeles Chargers", "Los Angeles Rams")
    assert (r["market_type"], r["line_value"], r["resolved_via"]) == ("total", 46.5, "slug")


def test_board_league_unknown_code_falls_back_to_llm():
    assert parse_polymarket_sports(_m("nfl-xyz-car-2026-10-25", "moneyline")) is None


def test_mlb_reuses_slug_parser():
    r = parse_polymarket_sports(_m("mlb-mil-lad-2026-10-15", "moneyline"))
    assert (r["away_team"], r["side"]) == ("Milwaukee Brewers", "Milwaukee Brewers")
    assert r["game_date"] == datetime(2026, 10, 15, tzinfo=UTC)  # slug day, not start time
    # Run lines: the slug parser skips them → LLM, as before.
    assert parse_polymarket_sports(_m("mlb-mil-lad-2026-10-15-spread-home-1pt5", "spreads")) is None


def test_other_sports_mapped_by_prefix():
    r = parse_polymarket_sports(_m("atp-sinner-alcaraz-2026-11-07", "moneyline", "Sinner vs. Alcaraz"))
    assert (r["sport"], r["market_class"], r["event"]) == ("tennis", "game", "Sinner vs. Alcaraz")


def test_esports_negative_cached():
    r = parse_polymarket_sports(_m("cs2-navi-vit-2026-11-07", "moneyline"))
    assert r["sport"] == NON_GAME_SENTINEL


def test_not_parseable_goes_to_llm():
    assert parse_polymarket_sports(_m("nfl-tb-car-2026-10-25", None)) is None  # futures etc.
    assert parse_polymarket_sports(_m("will-x-win-super-bowl", "moneyline")) is None
    assert parse_polymarket_sports(_m("nfl-tb-car-2026-10-25", "moneyline", venue="kalshi")) is None
