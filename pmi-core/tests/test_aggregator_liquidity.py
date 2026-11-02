"""Unit tests for CORR-3.4 quantile liquidity weighting in the aggregator.

Pure-Python, no DB / MLflow / network — the test builds ``MarketEvaluations``
directly with stub ``AuditEvaluation`` rows and asserts on the breakdown
``liquidity_weighting`` block plus the resulting score deltas.

Three behaviours we lock in:

1. **Cold start** (N < 4 samples or method=none) → uniform 1.0 weight, score
   matches the pre-CORR-3.4 formula. Guards against silent scoring drift
   for tiny indexes.
2. **Quantile method** with varied depths → applies the Micah-style ladder
   (0.90 / 1.00 / 1.20 / 1.50) so high-depth markets pull the index toward
   their direction × price.
3. **No-variance** (all depths identical) → falls back to uniform so a
   degenerate p20=p50=p80 boundary doesn't accidentally over-boost
   everything to the top bucket.
"""

from __future__ import annotations

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
    LiquidityWeighting,
    WeightingSpec,
)
from pmi_core.engine.aggregator import (
    MarketEvaluations,
    _liquidity_weights,
    _percentile,
    aggregate,
)
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir(method: str = "quantile") -> IndexDef:
    return IndexDef(
        id="liq-test",
        version=1,
        title="Liquidity Test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["x"])],
        factors=[
            FactorSpec(
                id="rel",
                type="binary",
                prompt_ref="prompts/factors/rel-v1",
                weight=1.0,
            ),
            FactorSpec(
                id="direction",
                type="ternary",
                prompt_ref="prompts/factors/direction-v1",
                weight=None,
            ),
        ],
        # Disable collapse so the aggregator math is decoupled from
        # date-aware bucket merging (which has its own test suite).
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False),
            min_components=1,
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method=method)),
    )


def _row(
    market_id: int,
    *,
    last_price: float,
    liquidity: float | None,
    rel: float = 1.0,
    direction: float = 1.0,
    play_money: bool = False,
) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id,
        venue="manifold" if play_money else "polymarket",
        external_id=f"ext-{market_id}",
        slug=None,
        title=f"market-{market_id}",
    )
    # is_play_money is a DB-generated column (None on a transient instance);
    # set it explicitly to mirror what the venue would generate in the DB.
    market.is_play_money = play_money
    rel_eval = AuditEvaluation(
        market_id=market_id,
        index_definition_id=1,
        factor_id="rel",
        prompt_id=1,
        prompt_sha256="x" * 64,
        model_id="stub",
        value_numeric=rel,
    )
    dir_eval = AuditEvaluation(
        market_id=market_id,
        index_definition_id=1,
        factor_id="direction",
        prompt_id=1,
        prompt_sha256="x" * 64,
        model_id="stub",
        value_numeric=direction,
    )
    return MarketEvaluations(
        market=market,
        by_factor={"rel": rel_eval, "direction": dir_eval},
        last_price=last_price,
        liquidity=liquidity,
    )


# ──────────────────────────────────────────────────────────────────────────
# Play-money exclusion — is_play_money markets stay members at neutral weight
# but are kept out of the quantile sample so fake volume can't define percentiles
# ──────────────────────────────────────────────────────────────────────────


def test_play_money_excluded_from_quantile_sample() -> None:
    """Play-money markets don't enter the percentile sample, but stay members
    weighted 1.0. With 4 real-money samples the ladder still applies to them."""
    real = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 5)]
    # Two play-money markets with huge "volume" that would distort percentiles.
    play = [
        _row(100, last_price=0.5, liquidity=1_000_000.0, play_money=True),
        _row(101, last_price=0.5, liquidity=2_000_000.0, play_money=True),
    ]
    weights, info = _liquidity_weights(real + play, _ir(method="quantile"))

    # Sample is the 4 real-money depths only — the two mana markets are excluded.
    assert info["sample_size"] == 4
    assert info["play_money_excluded"] == 2
    assert info["applied"] is True
    # Play-money markets stay present but at weight 0 (shown, never scored).
    assert weights[100] == 0.0
    assert weights[101] == 0.0
    # Percentiles were computed off 100..400, NOT off the millions — so the
    # top real-money market (400) lands in the top bucket, proving the huge
    # mana volumes never shifted the cut points.
    assert weights[4] == 1.50


def test_all_play_money_falls_back_to_uniform() -> None:
    """An index whose liquidity signal is entirely play-money has an empty
    sample → uniform weighting, never a mana-defined ladder."""
    rows = [
        _row(i, last_price=0.5, liquidity=float(i) * 1000, play_money=True)
        for i in range(1, 6)
    ]
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert all(w == 0.0 for w in weights.values())  # play-money never scores
    assert info["sample_size"] == 0
    assert info["applied"] is False


# ──────────────────────────────────────────────────────────────────────────
# _percentile helper — matches numpy 'linear' interpolation
# ──────────────────────────────────────────────────────────────────────────


def test_percentile_interpolates_between_ranks() -> None:
    vals = [10.0, 20.0, 30.0, 40.0]
    # p=0.5 over 4 sorted values lies between idx 1 (=20) and idx 2 (=30)
    assert _percentile(vals, 0.5) == 25.0


def test_percentile_handles_endpoints() -> None:
    vals = [1.0, 2.0, 3.0]
    assert _percentile(vals, 0.0) == 1.0
    assert _percentile(vals, 1.0) == 3.0
    assert _percentile([], 0.5) == 0.0


# ──────────────────────────────────────────────────────────────────────────
# _liquidity_weights — method=none / cold-start / quantile / linear
# ──────────────────────────────────────────────────────────────────────────


def test_method_none_returns_uniform() -> None:
    rows = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 11)]
    weights, info = _liquidity_weights(rows, _ir(method="none"))
    assert all(w == 1.0 for w in weights.values())
    assert info["method"] == "none"
    assert info["applied"] is False


def test_quantile_below_min_sample_returns_uniform() -> None:
    """3 markets is below the 4-sample floor → no quantile cut."""
    rows = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 4)]
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert all(w == 1.0 for w in weights.values())
    assert info["applied"] is False
    assert "sample_size<4" in info["reason"]


def test_quantile_no_variance_returns_uniform() -> None:
    """All depths identical → p20=p50=p80 collapse to a single point."""
    rows = [_row(i, last_price=0.5, liquidity=500.0) for i in range(1, 6)]
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert all(w == 1.0 for w in weights.values())
    assert info["applied"] is False
    assert "no variance" in info["reason"]


def test_quantile_applies_ladder() -> None:
    """Spread depths across the four buckets and check each market lands
    in the right tier."""
    # 10 depths 100..1000 step 100. With n=10, _percentile gives:
    #   p20 = 280  (interp between idx 1=200, idx 2=300, frac 0.8)
    #   p50 = 550
    #   p80 = 820
    # so we expect:
    #   <280 → bucket 0.90: depths 100, 200
    #   <550 → bucket 1.00: depths 300, 400, 500
    #   <820 → bucket 1.20: depths 600, 700, 800
    #   else → bucket 1.50: depths 900, 1000
    rows = [
        _row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 11)
    ]
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert info["applied"] is True
    assert info["sample_size"] == 10
    assert weights[1] == 0.90 and weights[2] == 0.90
    assert weights[3] == 1.00 and weights[4] == 1.00 and weights[5] == 1.00
    assert weights[6] == 1.20 and weights[7] == 1.20 and weights[8] == 1.20
    assert weights[9] == 1.50 and weights[10] == 1.50


def test_quantile_with_missing_signal_zeroes_that_market() -> None:
    """A market with None/0 liquidity weights 0 (member, shown, not scored) and is
    excluded from the quantile cut points so it doesn't drag p20 down to zero."""
    rows = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 6)]
    rows.append(_row(99, last_price=0.5, liquidity=None))
    rows.append(_row(100, last_price=0.5, liquidity=0.0))
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert info["sample_size"] == 5  # zero/None excluded from the ladder sample
    assert weights[99] == 0.0
    assert weights[100] == 0.0
    assert info["applied"] is True


# ──────────────────────────────────────────────────────────────────────────
# Unified liquidity policy (engine/liquidity.py) — NULL/0 → weight 0 (member,
# shown, does not score); positive → ladder; play-money → pm_w; a book with no
# positive signal still degrades to the uniform fallback, never zeros.
# ──────────────────────────────────────────────────────────────────────────


def test_nodata_zeroes_null_and_untraded() -> None:
    # 4 positive real-money → ladder applies; a NULL and a 0 → weight 0; a
    # play-money market stays a member at pm_w (1.0), never zeroed.
    rows = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 5)]
    rows.append(_row(99, last_price=0.5, liquidity=None))
    rows.append(_row(100, last_price=0.5, liquidity=0.0))
    rows.append(_row(200, last_price=0.5, liquidity=5000.0, play_money=True))
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert info["applied"] is True
    assert info["sample_size"] == 4                 # only the positive real-money
    assert weights[99] == 0.0 and weights[100] == 0.0   # NULL + untraded → 0
    assert weights[200] == 0.0                      # play-money never scores → 0
    assert weights[4] == 1.50                        # positive still laddered


def test_whole_book_no_signal_falls_back_uniform() -> None:
    """When NO market has a positive signal the ladder can't form → uniform
    fallback (1.0 everyone), NOT an all-zero book that would kill the score."""
    rows = [_row(i, last_price=0.5, liquidity=None) for i in range(1, 4)]
    rows.append(_row(4, last_price=0.5, liquidity=0.0))
    weights, info = _liquidity_weights(rows, _ir(method="quantile"))
    assert info["applied"] is False
    assert all(w == 1.0 for w in weights.values())


def test_linear_method_max_normalises() -> None:
    rows = [_row(i, last_price=0.5, liquidity=float(i) * 100) for i in range(1, 6)]
    weights, info = _liquidity_weights(rows, _ir(method="linear"))
    # max_d = 500. Weights = 0.5 + d/500 clamped to [0.5, 1.5].
    assert weights[1] == 0.5 + 100 / 500       # = 0.7
    assert weights[5] == 1.5                    # 0.5 + 500/500 = 1.5
    assert info["applied"] is True
    assert info["max_d"] == 500.0


# ──────────────────────────────────────────────────────────────────────────
# Integration: aggregate() respects the weights end-to-end
# ──────────────────────────────────────────────────────────────────────────


def test_aggregate_with_uniform_weights_matches_unweighted() -> None:
    """method=none must reproduce the pre-CORR-3.4 score so existing indexes
    that omit a weighting block don't silently drift."""
    rows = [
        _row(1, last_price=0.80, liquidity=None),
        _row(2, last_price=0.40, liquidity=None),
        _row(3, last_price=0.20, liquidity=None),
    ]
    # all relevancy=1, direction=+1, no liquidity weighting → raw = mean(price)
    result = aggregate(rows, _ir(method="none"))
    expected_raw = (0.80 + 0.40 + 0.20) / 3
    assert abs(result.breakdown["raw"] - expected_raw) < 1e-6
    assert result.score == round(50.0 + expected_raw * 50.0, 4)
    assert result.breakdown["liquidity_weighting"]["applied"] is False


def test_aggregate_boosts_high_depth_markets() -> None:
    """High-depth bullish markets should pull the index up vs uniform."""
    # 10 markets, all relevancy=1 direction=+1. Deeper markets are bullish
    # (price 0.9), shallower ones are bearish (price 0.1). With quantile
    # boost the deep bullish ones should dominate → raw shifts up.
    rows = []
    for i in range(1, 11):
        price = 0.9 if i >= 6 else 0.1  # 5 bull / 5 bear
        rows.append(_row(i, last_price=price, liquidity=float(i) * 100))

    weighted = aggregate(rows, _ir(method="quantile"))
    unweighted = aggregate(rows, _ir(method="none"))

    # Unweighted: simple mean of price (direction=+1) = (0.9*5 + 0.1*5)/10 = 0.5
    # → score = 50 + 0.5*50 = 75.0.
    assert abs(unweighted.breakdown["raw"] - 0.5) < 1e-6
    assert unweighted.score == 75.0

    # Weighted should tilt further bullish because higher-depth markets
    # carry the +0.9 price. Strict inequality vs uniform.
    assert weighted.score > unweighted.score
    assert weighted.breakdown["liquidity_weighting"]["applied"] is True


def test_aggregate_handles_cold_start_gracefully() -> None:
    """A mix of priced markets with no liquidity data shouldn't 500 or zero
    the score — they should just slip into the uniform fallback."""
    rows = [_row(i, last_price=0.6, liquidity=None) for i in range(1, 4)]
    result = aggregate(rows, _ir(method="quantile"))
    # All weights 1.0 → raw = 0.6, score = 50 + 30 = 80
    assert result.score == 80.0
    info = result.breakdown["liquidity_weighting"]
    assert info["applied"] is False
    assert info["sample_size"] == 0


# ──────────────────────────────────────────────────────────────────────────
# Null-score paths — backport of Micah PR #316 / job-executor PR #12
# (2026-05-28). The bug was countries below MIN_CONTRACTS keeping the last
# computed PMI visible to the UI; the fix persists NULL rather than letting
# a stale value linger. Equivalent triggers on this platform:
#   - below ``min_components`` after collapse
#   - zero weighted relevancy (no relevancy-weighted factor produced a value)
# ──────────────────────────────────────────────────────────────────────────


def _ir_min_components(n: int) -> IndexDef:
    base = _ir(method="none")
    base.aggregation.min_components = n
    return base


def test_aggregate_returns_null_below_min_components() -> None:
    """0 collapsed rows < min_components=1 → score is None, not 0.0."""
    result = aggregate([], _ir_min_components(1))
    assert result.score is None
    assert result.component_count == 0
    assert result.component_evaluation_ids == []
    assert result.breakdown["reason"] == "below min_components"
    assert result.breakdown["candidates"] == 0


def test_aggregate_returns_null_below_min_components_with_some_rows() -> None:
    """2 rows < min_components=3 still produces a null result so the API
    can render a gap rather than a misleading low score."""
    rows = [_row(i, last_price=0.6, liquidity=None) for i in range(1, 3)]
    result = aggregate(rows, _ir_min_components(3))
    assert result.score is None
    assert result.breakdown["reason"] == "below min_components"
    assert result.breakdown["candidates"] == 2


def test_component_count_counts_contributors_not_candidates() -> None:
    """Zero-relevancy candidates (selector noise the LLM gate culled) must
    not inflate component_count — it has to match the lineage /explain
    renders, with the full candidate tally kept in the breakdown."""
    rows = [
        _row(1, last_price=0.6, liquidity=None, rel=1.0),
        _row(2, last_price=0.4, liquidity=None, rel=1.0),
        _row(3, last_price=0.9, liquidity=None, rel=0.0),  # culled noise
    ]
    # _row leaves evaluation ids unset (transient ORM objects); assign them so
    # the lineage filter (`e.id is not None`) sees real rows.
    next_id = 1
    for r in rows:
        for e in r.by_factor.values():
            e.id = next_id
            next_id += 1
    result = aggregate(rows, _ir_min_components(1))
    assert result.score is not None
    assert result.component_count == 2
    # Lineage covers exactly the contributors: 2 markets × 2 factors.
    assert len(result.component_evaluation_ids) == 4
    assert result.breakdown["components_contributing"] == 2
    assert result.breakdown["components_after_collapse"] == 3


def test_aggregate_returns_null_when_no_relevancy_weighted_factor_evaluates() -> None:
    """Markets exist and pass min_components, but every relevancy-weighted
    factor is missing a numeric value → denominator stays 0 → score is None.
    Lineage IDs should still be empty (no evaluation contributed)."""
    market = CoreMarket(
        id=1,
        venue="polymarket",
        external_id="ext-1",
        slug=None,
        title="market-1",
    )
    # direction-only row: no `rel` evaluation at all, so `_relevancy`
    # returns 0 and the row is skipped — denominator never increments.
    dir_eval = AuditEvaluation(
        market_id=1,
        index_definition_id=1,
        factor_id="direction",
        prompt_id=1,
        prompt_sha256="x" * 64,
        model_id="stub",
        value_numeric=1.0,
    )
    row = MarketEvaluations(
        market=market,
        by_factor={"direction": dir_eval},
        last_price=0.5,
        liquidity=None,
    )
    result = aggregate([row], _ir_min_components(1))
    assert result.score is None
    # No market contributed (component_count counts contributors, not
    # candidates); the candidate tally moves to the breakdown.
    assert result.component_count == 0
    assert result.breakdown["reason"] == "zero relevancy across components"
    assert result.breakdown["candidates"] == 1
