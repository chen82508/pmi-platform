"""`_fold_moneyline`: both sides come from one P(home) fold, so an away-only venue
counts and the two sides sum to 100 (NFL board audit, 2026-10-29)."""

from __future__ import annotations

from pmi_core.engine.sports_compute import _fold_moneyline

HOME, AWAY = "Green Bay Packers", "Carolina Panthers"


def _leg(venue: str, side: str | None, price: float, vol: float, home: str = HOME) -> dict:
    return {"type": "moneyline", "venue": venue, "side": side, "home": home,
            "price": price, "vol": vol, "url": f"https://{venue}"}


def test_away_only_venue_is_folded_and_listed() -> None:
    mks = [
        _leg("kalshi", HOME, 0.65, 313_000),
        _leg("kalshi", AWAY, 0.36, 228_000),
        _leg("polymarket", AWAY, 0.355, 5_000),  # Polymarket prices outcome 0 only
    ]
    ml, venues = _fold_moneyline(mks, HOME, AWAY)
    assert {v["venue"] for v in venues} == {"kalshi", "polymarket"}
    poly = next(v for v in venues if v["venue"] == "polymarket")
    assert poly["pct"] == 64.5  # P(home) = 1 - 0.355
    assert ml[HOME] + ml[AWAY] == 100.0
    assert 64.5 <= ml[HOME] <= 65.0


def test_sideless_leg_is_its_own_home() -> None:
    ml, _ = _fold_moneyline([_leg("polymarket", None, 0.275, 250, home=AWAY)], HOME, AWAY)
    assert ml == {AWAY: 27.5, HOME: 72.5}


def test_non_moneyline_and_foreign_sides_ignored() -> None:
    mks = [
        {**_leg("kalshi", HOME, 0.51, 1), "type": "spread"},
        _leg("kalshi", "Detroit Lions", 0.9, 1),
    ]
    assert _fold_moneyline(mks, HOME, AWAY) == ({}, [])
