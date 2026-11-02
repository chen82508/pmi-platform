"""Slim seat-board history keys and payload (pmi_core.engine.board_trend)."""

from __future__ import annotations

import pytest

from pmi_core.engine.board_trend import (
    board_key,
    slim_payload,
    trend_key_for_board_key,
)

# The seat family's trend fields (authoritative source: trend_store's senate-board spec).
TREND_FIELDS = ("p_r_control", "prob_by_state", "prob_by_district", "version")


def test_keys_pair_up():
    idx = "us-house-2026-republican-seats"
    assert board_key(idx) == "senate-board:us-house-2026-republican-seats"
    assert trend_key_for_board_key(board_key(idx)) == "senate-board-trend:us-house-2026-republican-seats"


def test_trend_key_rejects_other_cache_keys():
    with pytest.raises(ValueError):
        trend_key_for_board_key("maga:2026")


def test_slim_payload_keeps_only_history_fields():
    full = {
        "p_r_control": 41.2,
        "prob_by_state": {"TX": 0.4},
        "prob_by_district": {"NJ-12": 0.1},
        "version": 9,
        "races": [{"state": "NJ-12"}] * 3,
        "counts": {"tossup": 4},
    }
    slim = slim_payload(full)
    assert set(slim) == set(TREND_FIELDS)
    assert "races" not in slim and slim["prob_by_district"] == {"NJ-12": 0.1}


def test_slim_payload_omits_fields_a_board_does_not_have():
    # senate/governor boards carry no prob_by_district
    assert slim_payload({"p_r_control": 50.0, "prob_by_state": {}, "version": 1}) == {
        "p_r_control": 50.0,
        "prob_by_state": {},
        "version": 1,
    }
