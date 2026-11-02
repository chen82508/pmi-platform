"""`_settled_twin` — a settled fixture must not also sit in the live list.

The 2026-09-14 board showed Bushido vs ENJOY twice: its resolved legs in
"Recently settled" and its still-open Polymarket leg in the upcoming list,
frozen at 100%/0%. Identity mirrors build_matches' own clustering: (game,
team-key pair) with start times inside the ±60h cross-venue tolerance.
"""

from __future__ import annotations

from pmi_core.engine.esports_compute import _settled_twin


def _m(
    game: str = "cs2",
    team_a: str = "Bushido Wildcats",
    team_b: str = "ENJOY",
    starts_at: str | None = "2026-09-14T08:00:00+00:00",
) -> dict:
    return {"game": game, "team_a": team_a, "team_b": team_b, "starts_at": starts_at}


SETTLED = [_m()]


def test_same_fixture_is_twin():
    assert _settled_twin(_m(), SETTLED)


def test_cross_venue_time_gap_within_window_is_twin():
    # Venues log the same match's times up to ~2 days apart (the systematic
    # closes_at convention gap) — still the one fixture.
    assert _settled_twin(_m(starts_at="2026-09-16T06:00:00+00:00"), SETTLED)


def test_rematch_outside_window_stays_live():
    assert not _settled_twin(_m(starts_at="2026-09-19T08:00:00+00:00"), SETTLED)


def test_reversed_and_renamed_teams_still_match():
    # Pair is a set, and keys — not display names — are the identity: case and
    # punctuation variants fold to the same team key.
    assert _settled_twin(_m(team_a="enjoy!", team_b="bushido wildcats"), SETTLED)


def test_different_game_is_not_twin():
    assert not _settled_twin(_m(game="valorant"), SETTLED)


def test_different_pair_is_not_twin():
    assert not _settled_twin(_m(team_b="NAVI Junior"), SETTLED)


def test_missing_start_time_counts_as_twin():
    # Inside the 72h settled window an undated leg is the duplicate, not a
    # dated future rematch — dropping it is the safe reading.
    assert _settled_twin(_m(starts_at=None), SETTLED)
    assert _settled_twin(_m(), [_m(starts_at=None)])
    # An unparseable timestamp reads as missing, not as a different fixture.
    assert _settled_twin(_m(starts_at="not-a-date"), SETTLED)
