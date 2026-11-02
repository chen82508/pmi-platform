"""`_resolve_swiss` record-flow bucketing.

The 2026-10-24 Worlds board showed a bloated 0-0 column: two round-2 pairings
(e.g. Karmine Corp 1-0 vs LYON 0-0) leaked into it because their shared round-1
match had not settled upstream on Kalshi yet, so the two teams still carried
different records and the old `min(record)` bucketing dumped the live pairing
into the lower record column. A valid Swiss pairing always has both teams at the
same record going in, so a mismatched-record live pairing is premature and must
be hidden, not mis-bucketed.
"""

from __future__ import annotations

from pmi_core.engine.worlds_compute import _resolve_swiss

_NAMES = {"A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta"}
_STAGE = {
    "teams": ["A", "B", "C", "D"],
    "round1": [{"a": "A", "b": "B"}, {"a": "C", "b": "D"}],
    "advance_wins": 3,
    "eliminate_losses": 3,
    "rounds": 5,
    "advances": 2,
}


def _res(team_a: str, team_b: str, winner: str, resolved_at: str) -> dict:
    return {
        "team_a": team_a,
        "team_b": team_b,
        "winner": winner,
        "resolved_at": resolved_at,
        "slug": f"{team_a}-{team_b}".lower(),
    }


def _live(team_a: str, team_b: str) -> dict:
    return {
        "team_a": team_a,
        "team_b": team_b,
        "prob_a": 0.5,
        "slug": f"{team_a}-{team_b}".lower(),
        "starts_at": None,
    }


def _columns(out: dict) -> dict[str, list[dict]]:
    return {c["record"]: c["matches"] for c in out["columns"]}


# Round 1 done: Alpha & Charlie are 1-0, Bravo & Delta are 0-1.
_RESULTS = [
    _res("Alpha", "Bravo", "Alpha", "2026-10-24T01:00:00+00:00"),
    _res("Charlie", "Delta", "Charlie", "2026-10-24T02:00:00+00:00"),
]


def test_resolved_round1_sits_in_0_0():
    out = _resolve_swiss(_STAGE, [], _RESULTS, _NAMES)
    cols = _columns(out)
    assert len(cols["0-0"]) == 2
    assert all(m["status"] == "resolved" for m in cols["0-0"])


def test_matched_record_live_pairing_shows():
    # Alpha (1-0) vs Charlie (1-0) is a valid round-2 pairing.
    out = _resolve_swiss(_STAGE, [_live("Alpha", "Charlie")], _RESULTS, _NAMES)
    cols = _columns(out)
    assert len(cols["1-0"]) == 1
    assert cols["1-0"][0]["status"] == "live"


def test_premature_mismatched_pairing_is_hidden():
    # Alpha (1-0) vs Delta (0-1): not a real Swiss pairing yet — must not appear
    # in any column (the old min() bug dumped it into 0-0).
    out = _resolve_swiss(_STAGE, [_live("Alpha", "Delta")], _RESULTS, _NAMES)
    shown = {frozenset({m["team_a"], m["team_b"]}) for ms in _columns(out).values() for m in ms}
    assert frozenset({"Alpha", "Delta"}) not in shown
    assert len(_columns(out)["0-0"]) == 2  # unchanged by the premature pairing
