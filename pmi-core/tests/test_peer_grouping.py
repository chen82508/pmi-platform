"""Unit tests for cross-venue peer grouping (engine/peer_grouping.py).

`group_peers` itself needs pgvector and is covered by the pmi-api route test
(test_routes_explain_peers.py); here we pin the pure union-find that turns
similarity edges into stable group ids.
"""

from __future__ import annotations

from pmi_core.engine.peer_grouping import union_groups


def test_singletons_map_to_themselves():
    assert union_groups([3, 1, 2], []) == {1: 1, 2: 2, 3: 3}


def test_pair_collapses_to_smaller_id():
    got = union_groups([10, 20, 30], [(20, 10)])
    assert got == {10: 10, 20: 10, 30: 30}


def test_transitive_chain_one_group():
    # a—b and b—c ⇒ {a, b, c} share one group rooted at the smallest id.
    got = union_groups([1, 2, 3, 4], [(2, 3), (1, 2)])
    assert got == {1: 1, 2: 1, 3: 1, 4: 4}


def test_edges_outside_id_set_ignored():
    got = union_groups([1, 2], [(1, 99), (98, 2), (1, 2)])
    assert got == {1: 1, 2: 1}


def test_two_independent_groups():
    got = union_groups([1, 2, 3, 4], [(1, 3), (2, 4)])
    assert got == {1: 1, 3: 1, 2: 2, 4: 2}


def test_empty_and_single():
    assert union_groups([], []) == {}
    assert union_groups([7], []) == {7: 7}
