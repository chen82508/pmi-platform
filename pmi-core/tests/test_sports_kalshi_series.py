"""`_kalshi_series`: round + teams from the KXMLBSERIES ticker. Kalshi titles the
World Series and both LCS "Championship Series winner", so the LLM event label
filed the 2026 WS series market under League Championship."""

from __future__ import annotations

import pytest

from pmi_core.engine.sports_compute import _kalshi_series


@pytest.mark.parametrize(
    ("ext", "round_", "team", "opp"),
    [
        ("KXMLBSERIES-26TBMILWS-MIL", "World Series", "Milwaukee Brewers", "Tampa Bay Rays"),
        ("KXMLBSERIES-26LADMILNLCS-LAD", "League Championship", "Los Angeles Dodgers", "Milwaukee Brewers"),
        ("KXMLBSERIES-26CWSCLEALDS-CWS", "Division Series", "Chicago White Sox", "Cleveland Guardians"),
        ("KXMLBSERIES-26BOSNYYWC-NYY", "Wild Card", "New York Yankees", "Boston Red Sox"),
    ],
)
def test_round_and_teams(ext: str, round_: str, team: str, opp: str) -> None:
    s = _kalshi_series(ext)
    assert (s.round, s.team, s.opponent) == (round_, team, opp)


@pytest.mark.parametrize(
    "ext",
    [None, "KXMLBSERIESSCORE-26TBMILWS-MIL4", "KXMLB-26-MIL", "KXMLBSERIES-26TBXXXWS-TB"],
)
def test_non_series_or_unknown_codes(ext: str | None) -> None:
    assert _kalshi_series(ext) is None
