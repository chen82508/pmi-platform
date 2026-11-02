"""Replaying a labeller's stored votes as a system under test.

This is how a factor's model binding gets chosen without paying for the
experiment: labelling already asked every candidate the membership question on
every gold item, so "how would this model score in production" is a lookup.

The subtlety worth pinning is the abstain case. A model that returned nothing
must not silently vanish from the comparison — if it were dropped, a labeller
that abstained on everything hard would post a better score than one that
answered. ``metrics`` already treats an absent prediction as a negative, so what
this module has to guarantee is only that "voted, but abstained" and "never
voted" stay distinguishable at this layer.
"""

from __future__ import annotations

from pmi_core.eval.schema import GoldItem, GoldSpec, Vote
from pmi_core.eval.systems import labeller_predictions, labeller_sources

SPEC = GoldSpec(
    taxonomy="btc-membership",
    version=1,
    index_id="btc-bull-bear-index",
    questions=("btc_price_relevance",),
    membership_question="btc_price_relevance",
)


def _item(market_id: int, votes: list[Vote] | None) -> GoldItem:
    return GoldItem(
        market_id=market_id,
        venue="kalshi",
        title=f"market {market_id}",
        stratum="candidate_positive",
        votes={} if votes is None else {"btc_price_relevance": votes},
    )


def test_reads_only_the_named_source():
    items = [
        _item(
            1,
            [
                Vote(source="gpt-4o-mini-2024-07-18", value=1.0),
                Vote(source="ollama/gemma3:4b", value=0.0),
                Vote(source="structural", value=1.0),
            ],
        )
    ]
    assert labeller_predictions(SPEC, items, "gpt-4o-mini-2024-07-18") == {1: 1.0}
    assert labeller_predictions(SPEC, items, "ollama/gemma3:4b") == {1: 0.0}
    assert labeller_predictions(SPEC, items, "structural") == {1: 1.0}


def test_self_consistency_samples_are_averaged():
    """Two of three samples said yes — 0.667, which the 0.5 threshold calls in.

    Averaging rather than majority-voting keeps the margin visible, so a
    threshold sweep can later ask whether 0.5 is the right cut.
    """
    items = [
        _item(
            1,
            [
                Vote(source="m", value=1.0),
                Vote(source="m", value=1.0),
                Vote(source="m", value=0.0),
            ],
        )
    ]
    assert labeller_predictions(SPEC, items, "m") == {1: 2 / 3}


def test_abstention_is_omitted_not_zeroed():
    """An abstain leaves the market out of the mapping entirely.

    ``metrics`` counts a missing prediction as a negative AND reports it under
    ``n_missing_prediction``, which is the distinction that matters: a model
    that declined is not the same as a model that said no, and collapsing the
    two here would hide it from the report.
    """
    items = [_item(1, [Vote(source="m", value=None)]), _item(2, [Vote(source="m", value=1.0)])]
    assert labeller_predictions(SPEC, items, "m") == {2: 1.0}


def test_unknown_source_yields_nothing_and_sources_are_discoverable():
    items = [
        _item(1, [Vote(source="gpt-4o-mini-2024-07-18", value=1.0)]),
        _item(2, [Vote(source="structural", value=0.0)]),
        _item(3, None),
    ]
    assert labeller_predictions(SPEC, items, "claude-nope") == {}
    assert labeller_sources(SPEC, items) == ["gpt-4o-mini-2024-07-18", "structural"]
