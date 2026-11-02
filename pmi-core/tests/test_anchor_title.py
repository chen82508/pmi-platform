"""War anchor headlines must keep the facing contract's polarity."""

from __future__ import annotations

import pytest

from pmi_core.engine.anchor_title import polarity_matches, title_polarity


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Will Saudi Arabia and the Houthis escalate their conflict?", 1),
        ("Will China invade Taiwan?", 1),
        ("Will Iran attack or occupy Kuwait?", 1),
        ("Houthi x Saudi Arabia effective ceasefire by October 31?", -1),
        ("US x Iran ceasefire continues through October 31?", -1),
        ("Will the US and Iran achieve a lasting resolution?", -1),
        ("Will the U.S. lift CAATSA sanctions on Turkey by October 31, 2026?", -1),
        ("Will Iran target Iraq by October 31, 2026?", 1),
        ("Iran – Iraq", None),
        ("Will there be a ceasefire after the next attack?", None),  # both → ambiguous
    ],
)
def test_title_polarity(title, expected):
    assert title_polarity(title) == expected


def test_polarity_matches_rejects_only_confident_contradictions():
    # ceasefire contract (direction -1) fronted by an "escalate?" headline → rejected
    assert not polarity_matches("Will Saudi Arabia and the Houthis escalate their conflict?", -1)
    # same polarity → kept
    assert polarity_matches("Will the US and Iran achieve a lasting resolution?", -1)
    assert polarity_matches("Will Iran attack or occupy Kuwait?", 1)
    # unknown on either side → kept (never drop a headline on a guess)
    assert polarity_matches("Iran – Iraq", 1)
    assert polarity_matches("Will China invade Taiwan?", None)
    assert polarity_matches("Will China invade Taiwan?", 0)
