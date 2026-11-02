"""Riot's schedule (the tournament YAML) over the venues' stale starts.

2026-10-28: Riot swapped two Swiss round-4 slots after the venues listed them.
Kalshi's tickers (KXLOLGAME-26OCT291600T1MKOI, -26OCT281600BLGTES) kept the
listing-time dates, so /micah/lol-worlds put T1 vs Movistar KOI a day late and
Top Esports vs Bilibili Gaming a day early — and, with Polymarket's BLG–TES
closing 33h after Kalshi's stale start, split that one fixture into two matches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.engine.esports_matches import Mkt, _team_key, build_matches
from pmi_core.engine.worlds_compute import (
    _apply_official_starts,
    _official_start_lookup,
    load_tournament,
)

_NAMES = {"MKOI": "Movistar KOI", "T1": "T1", "BLG": "Bilibili Gaming", "TES": "Top Esports"}
_SCHEDULE = [
    {"a": "MKOI", "b": "T1", "starts_at": "2026-10-28T20:00:00Z"},
    {"a": "BLG", "b": "TES", "starts_at": "2026-10-29T23:00:00Z"},
]
_LOOKUP = _official_start_lookup(_SCHEDULE, _NAMES)


def _live(team_a: str, team_b: str, starts_at: str | None) -> dict:
    return {"team_a": team_a, "team_b": team_b, "starts_at": starts_at, "slug": "keep-me"}


def test_riot_time_replaces_the_stale_venue_time():
    m = _live("T1", "Movistar KOI", "2026-10-29T20:00:00+00:00")
    _apply_official_starts([m], _LOOKUP)
    assert m["starts_at"] == "2026-10-28T20:00:00+00:00"
    assert m["slug"] == "keep-me"  # the match page URL is already linked


def test_pair_order_does_not_matter():
    m = _live("Top Esports", "Bilibili Gaming", "2026-10-28T20:00:00+00:00")
    _apply_official_starts([m], _LOOKUP)
    assert m["starts_at"] == "2026-10-29T23:00:00+00:00"


def test_entry_beyond_tolerance_is_another_match():
    m = _live("T1", "Movistar KOI", "2026-11-07T20:00:00+00:00")
    _apply_official_starts([m], _LOOKUP)
    assert m["starts_at"] == "2026-11-07T20:00:00+00:00"


def test_unscheduled_or_untimed_match_is_left_alone():
    a = _live("T1", "Bilibili Gaming", "2026-10-31T20:00:00+00:00")
    b = _live("T1", "Movistar KOI", None)
    _apply_official_starts([a, b], _LOOKUP)
    assert a["starts_at"] == "2026-10-31T20:00:00+00:00"
    assert b["starts_at"] is None


def _kalshi(mid: int, winner: str, code: str, price: float) -> Mkt:
    # Kalshi's stale listing: Oct 28 16:00 ET = 20:00 UTC; closes ~2 days later.
    return Mkt(
        market_id=mid, venue="kalshi",
        title=f"Will {winner} win the Bilibili Gaming vs. Top Esports League of Legends match?",
        external_id=f"KXLOLGAME-26OCT281600BLGTES-{code}", event_key="KXLOLGAME-26OCT281600BLGTES",
        group_item=None, price=price, volume_24h=18_000.0,
        closes_at=datetime(2026, 10, 30, 20, tzinfo=UTC), url=None, rules="Worlds 2026",
        match_time=None,
    )


def _poly(mid: int, price: float) -> Mkt:
    # Polymarket closes ~at the match end: 33h after Kalshi's stale start.
    return Mkt(
        market_id=mid, venue="polymarket",
        title="LoL: Top Esports vs Bilibili Gaming (BO3) - Worlds Group Stage",
        external_id=str(mid), event_key=None, group_item="Match Winner", price=price,
        volume_24h=25_000.0, closes_at=datetime(2026, 10, 30, 5, tzinfo=UTC), url=None,
        rules=None, match_time=None,
    )


_BLG_TES = [_kalshi(1, "Top Esports", "TES", 0.24), _kalshi(2, "Bilibili Gaming", "BLG", 0.77), _poly(3, 0.245)]


def test_without_the_schedule_a_reshuffled_fixture_splits():
    assert len(build_matches(_BLG_TES)) == 2


def test_schedule_folds_a_reshuffled_fixture_into_one_match():
    ms = build_matches(_BLG_TES, _LOOKUP)
    assert len(ms) == 1, ms
    assert ms[0]["venues"] == ["kalshi", "polymarket"]
    assert sorted(ms[0]["market_ids"]) == [1, 2, 3]


def test_lookup_needs_a_venue_time_within_tolerance():
    keys = (_team_key("Top Esports"), _team_key("Bilibili Gaming"))
    official = datetime(2026, 10, 29, 23, tzinfo=UTC)
    assert _LOOKUP(*keys, official - timedelta(hours=27)) == official
    assert _LOOKUP(*keys, official + timedelta(days=4)) is None
    assert _LOOKUP(*keys, None) is None


def test_schedule_codes_are_on_the_roster():
    tour = load_tournament()
    codes = {t["code"] for t in tour["teams"]}
    for e in tour.get("schedule") or []:
        assert {e["a"], e["b"]} <= codes, e
