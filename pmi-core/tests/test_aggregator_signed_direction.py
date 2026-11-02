"""Unit tests for the BTC Bull/Bear tune (2026-06-24):

1. **Direction-by-type** — the aggregator must read the index's ternary factor by
   TYPE, not the hardcoded id ``"direction"``. An index whose direction factor is
   ``btc_direction`` previously defaulted every market to +1, nullifying its
   bull/bear signal. We lock in that a -1 direction now actually flips the term.

2. **Centered formula** — ``signed_weighted_average_x_100`` maps a YES price of
   0.5 to a *neutral* contribution (0), so a balanced book sits at 50. The legacy
   ``weighted_average_x_100`` keeps the raw price term (0.5 → +0.5).

Pure-Python: builds ``MarketEvaluations`` with stub ``AuditEvaluation`` rows and
asserts on the score, no DB / network.
"""

from __future__ import annotations

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
)
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir(*, formula: str, direction_id: str = "btc_direction") -> IndexDef:
    return IndexDef(
        id="btc-test",
        version=1,
        title="BTC Test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["btc"])],
        factors=[
            FactorSpec(id="rel", type="binary", prompt_ref="prompts/factors/rel-v1", weight=1.0),
            FactorSpec(
                id=direction_id,
                type="ternary",
                prompt_ref="prompts/factors/dir-v1",
                weight=None,
            ),
        ],
        # Collapse off so the math is decoupled from date-bucket merging.
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False), min_components=1, formula=formula
        ),
    )


def _row(
    market_id: int, *, last_price: float, direction: float, direction_id: str = "btc_direction"
) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id, venue="polymarket", external_id=f"ext-{market_id}", slug=None,
        title=f"market-{market_id}",
    )
    market.is_play_money = False

    def _ev(factor_id: str, value: float) -> AuditEvaluation:
        return AuditEvaluation(
            market_id=market_id, index_definition_id=1, factor_id=factor_id,
            prompt_id=1, prompt_sha256="x" * 64, model_id="stub", value_numeric=value,
        )

    return MarketEvaluations(
        market=market,
        by_factor={"rel": _ev("rel", 1.0), direction_id: _ev(direction_id, direction)},
        last_price=last_price,
        liquidity=None,  # no signal → uniform 1.0 weight, isolates the formula
    )


def test_direction_read_by_type_not_hardcoded_id() -> None:
    """A -1 ``btc_direction`` must flip the term. Two mirror markets at price 0.8
    (one +1, one -1) cancel to a neutral 50 — proving the custom-id direction is
    actually consumed. Pre-fix they were both treated +1 → score 90."""
    rows = [
        _row(1, last_price=0.8, direction=1.0),
        _row(2, last_price=0.8, direction=-1.0),
    ]
    result = aggregate(rows, _ir(formula="weighted_average_x_100"))
    assert result.score == 50.0


def test_signed_formula_centers_coinflip_at_50() -> None:
    """Balanced 'Up or Down' markets priced 0.50, direction +1 → neutral 50."""
    rows = [_row(i, last_price=0.5, direction=1.0) for i in range(1, 5)]
    result = aggregate(rows, _ir(formula="signed_weighted_average_x_100"))
    assert result.score == 50.0
    assert result.breakdown["centered"] is True


def test_signed_formula_bullish_and_bearish() -> None:
    """Price 0.75 dir +1 → +0.5 → 75; same price dir -1 → -0.5 → 25."""
    bull = aggregate(
        [_row(1, last_price=0.75, direction=1.0)],
        _ir(formula="signed_weighted_average_x_100"),
    )
    bear = aggregate(
        [_row(1, last_price=0.75, direction=-1.0)],
        _ir(formula="signed_weighted_average_x_100"),
    )
    assert bull.score == 75.0
    assert bear.score == 25.0


def test_legacy_formula_keeps_raw_price_term() -> None:
    """Same 0.50/+1 market under the default formula stays +0.5 → 75 (un-centered),
    so existing indexes (war) are unaffected."""
    rows = [_row(i, last_price=0.5, direction=1.0) for i in range(1, 5)]
    result = aggregate(rows, _ir(formula="weighted_average_x_100"))
    assert result.score == 75.0
    assert result.breakdown["centered"] is False


def test_legacy_direction_id_still_found_by_type() -> None:
    """War's factor id 'direction' is still located (it's the ternary factor),
    so the by-type lookup is backward compatible."""
    rows = [
        _row(1, last_price=0.8, direction=1.0, direction_id="direction"),
        _row(2, last_price=0.8, direction=-1.0, direction_id="direction"),
    ]
    result = aggregate(rows, _ir(formula="weighted_average_x_100", direction_id="direction"))
    assert result.score == 50.0


def _row_without(
    market_id: int, *, last_price: float | None, drop_direction: bool = False
) -> MarketEvaluations:
    """A row missing its direction eval and/or price snapshot."""
    row = _row(market_id, last_price=0.9, direction=1.0)
    if drop_direction:
        del row.by_factor["btc_direction"]
    row.last_price = last_price
    return row


def test_missing_direction_eval_excludes_market() -> None:
    """An index that declared a ternary factor must EXCLUDE a market whose
    direction eval is missing — not count it as +1 (bullish/escalation bias)
    nor dilute the denominator as an evaluated-neutral 0."""
    rows = [
        _row(1, last_price=0.75, direction=1.0),
        _row_without(2, last_price=0.9, drop_direction=True),  # would drag up if +1
    ]
    result = aggregate(rows, _ir(formula="signed_weighted_average_x_100"))
    assert result.score == 75.0  # identical to market 1 alone
    assert result.breakdown["skipped_no_direction"] == 1
    assert result.component_count == 1


def test_missing_price_skips_market_not_imputed() -> None:
    """A price-less market carries no signal — skipping beats imputing a fake
    0.5 (which read as a genuine 50 % under the unsigned formula)."""
    rows = [
        _row(1, last_price=0.8, direction=1.0),
        _row_without(2, last_price=None),
    ]
    result = aggregate(rows, _ir(formula="weighted_average_x_100"))
    assert result.score == 90.0  # 50 + 0.8×50 from market 1 alone
    assert result.breakdown["skipped_no_price"] == 1
    assert result.component_count == 1
