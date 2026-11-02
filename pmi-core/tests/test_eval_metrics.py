"""Gold-set scoring math.

The property that matters most here is that a stratified sample cannot be read
at face value: a classifier can look excellent on the sample and be terrible on
the population, and the whole point of carrying strata populations around is to
make that visible.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pmi_core.eval.metrics import compare, evaluate_membership, liquidity_weights
from pmi_core.eval.schema import (
    GoldItem,
    GoldSetMeta,
    GoldSpec,
    StratumMeta,
)

SPEC = GoldSpec(
    taxonomy="btc-membership",
    version=1,
    index_id="btc-bull-bear-index",
    questions=("btc_price_relevance",),
    membership_question="btc_price_relevance",
)


def _item(
    market_id: int,
    stratum: str,
    gold: float | None,
    *,
    liquidity: float | None = None,
    status: str = "labelled",
    is_play_money: bool = False,
) -> GoldItem:
    return GoldItem(
        market_id=market_id,
        venue="kalshi",
        title=f"market {market_id}",
        stratum=stratum,  # type: ignore[arg-type]
        labels={} if gold is None else {"btc_price_relevance": gold},
        status=status,  # type: ignore[arg-type]
        labelled_at=datetime.now(UTC),
        liquidity=liquidity,
        is_play_money=is_play_money,
    )


def _meta(**strata: tuple[int, int]) -> GoldSetMeta:
    return GoldSetMeta(
        taxonomy="btc-membership",
        version=1,
        index_id="btc-bull-bear-index",
        created_at=datetime.now(UTC),
        strata={
            name: StratumMeta(population=pop, sampled=n)
            for name, (pop, n) in strata.items()
        },
    )


def test_population_view_exposes_what_the_sample_view_hides() -> None:
    """Perfect on the dense stratum, 2 FPs on the huge one → sample looks fine,
    population precision collapses. This is why the sidecar exists."""
    items = [_item(i, "candidate_positive", 1.0) for i in range(10)]
    items += [_item(100 + i, "corpus_random", 0.0) for i in range(10)]

    preds = {i: 1.0 for i in range(10)}
    preds.update({100: 1.0, 101: 1.0})  # two false positives out in the corpus

    meta = _meta(candidate_positive=(1_000, 10), corpus_random=(1_000_000, 10))
    rep = evaluate_membership(SPEC, meta, items, preds, system="test")

    assert rep.sample.tp == 10 and rep.sample.fp == 2
    assert rep.sample.precision == pytest.approx(10 / 12)

    # Each corpus_random item stands for 100_000 markets; each candidate_positive
    # for 1_000. 2 FPs therefore outweigh 10 TPs by two orders of magnitude.
    assert rep.population.precision is not None
    assert rep.population.precision < 0.01
    assert rep.population.recall == 1.0


def test_missing_prediction_counts_as_a_miss() -> None:
    """A member the pipeline never even looked at is a false negative.

    Skipping unpredicted items would let a selector that returns nothing score
    perfect precision.
    """
    items = [_item(1, "near_miss", 1.0), _item(2, "near_miss", 1.0)]
    meta = _meta(near_miss=(100, 2))

    rep = evaluate_membership(SPEC, meta, items, {1: 1.0}, system="test")

    assert rep.n_missing_prediction == 1
    assert rep.sample.tp == 1 and rep.sample.fn == 1
    assert rep.sample.recall == 0.5


def test_contested_items_are_excluded_not_counted_either_way() -> None:
    """Labeller disagreement is a rubric gap, not a classifier error."""
    items = [
        _item(1, "candidate_zero", 1.0),
        _item(2, "candidate_zero", None, status="contested"),
    ]
    meta = _meta(candidate_zero=(50, 2))

    rep = evaluate_membership(SPEC, meta, items, {1: 1.0, 2: 1.0}, system="test")

    assert rep.n_contested == 1
    assert rep.n_scored == 1
    assert rep.sample.tp == 1
    assert rep.sample.fp == 0  # the contested item did NOT become a false positive


def test_impact_view_weights_errors_by_book_depth() -> None:
    """One FP on a deep book must outweigh one FP on an untraded strike."""
    items = [
        _item(1, "candidate_zero", 0.0, liquidity=1.0),
        _item(2, "candidate_zero", 0.0, liquidity=10.0),
        _item(3, "candidate_zero", 0.0, liquidity=100.0),
        _item(4, "candidate_zero", 0.0, liquidity=1_000_000.0),
    ]
    meta = _meta(candidate_zero=(4, 4))  # weight 1.0 → isolates the liquidity effect

    thin = evaluate_membership(SPEC, meta, items, {1: 1.0}, system="thin")
    deep = evaluate_membership(SPEC, meta, items, {4: 1.0}, system="deep")

    assert thin.population.fp == deep.population.fp == 1.0
    assert deep.impact.fp > thin.impact.fp


def test_liquidity_ladder_falls_back_to_uniform_without_variance() -> None:
    items = [_item(i, "candidate_zero", 0.0, liquidity=5.0) for i in range(6)]
    assert set(liquidity_weights(items).values()) == {1.0}


def test_liquidity_ladder_ignores_play_money_in_the_sample() -> None:
    """Play-money volume is site credits — it must not define the percentiles."""
    real = [_item(i, "candidate_zero", 0.0, liquidity=float(i + 1)) for i in range(4)]
    play = [_item(90, "candidate_zero", 0.0, liquidity=1e9, is_play_money=True)]
    weights = liquidity_weights(real + play)
    assert weights[90] == 1.0  # weighted, but never in the top bucket


def test_compare_flags_an_impact_regression_even_when_counts_improve() -> None:
    items = [
        _item(1, "candidate_zero", 0.0, liquidity=1.0),
        _item(2, "candidate_zero", 0.0, liquidity=2.0),
        _item(3, "candidate_zero", 0.0, liquidity=3.0),
        _item(4, "candidate_positive", 1.0, liquidity=1_000_000.0),
    ]
    meta = _meta(candidate_zero=(3, 3), candidate_positive=(1, 1))

    # Baseline: two cheap false positives, but it finds the deep member.
    base = evaluate_membership(
        SPEC, meta, items, {1: 1.0, 2: 1.0, 4: 1.0}, system="baseline"
    )
    # Candidate: no false positives at all, but it drops the deep member.
    cand = evaluate_membership(SPEC, meta, items, {}, system="candidate")

    out = compare(base, cand)
    assert out["regression"] is not None
    assert any("recall" in r for r in out["regression"])
