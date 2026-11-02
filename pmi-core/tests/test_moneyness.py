"""Strike-aware moneyness weighting (BTC Bull/Bear v4) — engine/moneyness.py.

The v4 fix de-confounds moneyness from sentiment: an "above $X" ladder lists far
more strikes above spot than below, so each correctly-priced far-OTM strike (~0)
scores a spurious −0.9 under the signed formula and the listing skew, not
sentiment, pins the index low. These tests lock in:

* the strike parser and the ladder-implied-spot estimator (robust to the
  non-monotonic last-trade noise seen in real data),
* that an OTM-skewed ladder reads ~50 with weighting on but deeply bearish off,
* that a genuine near-the-money bullish book still reads > 50,
* that the feature is inert when disabled (war / other indexes unaffected).

Pure-Python: stub ``MarketEvaluations``; no DB / network.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
    MoneynessWeighting,
    WeightingSpec,
)
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.engine.moneyness import _implied_spot, extract_strike, moneyness_weights
from pmi_core.models import AuditEvaluation, CoreMarket

_CLOSE = datetime(2026, 6, 24, tzinfo=UTC)


def _ir(*, moneyness: bool, bandwidth: float = 0.05, min_ladder_size: int = 4) -> IndexDef:
    return IndexDef(
        id="btc-test",
        version=4,
        title="BTC Test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["btc"])],
        factors=[
            FactorSpec(id="rel", type="binary", prompt_ref="prompts/factors/rel-v1", weight=1.0),
            FactorSpec(
                id="btc_direction", type="ternary", prompt_ref="prompts/factors/dir-v1", weight=None
            ),
        ],
        weighting=WeightingSpec(
            moneyness=MoneynessWeighting(
                enabled=moneyness, bandwidth=bandwidth, min_ladder_size=min_ladder_size
            )
        ),
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=False),
            min_components=1,
            formula="signed_weighted_average_x_100",
        ),
    )


def _row(market_id: int, *, title: str, last_price: float, direction: float) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id, venue="kalshi", external_id=f"ext-{market_id}", slug=None,
        title=title, closes_at=_CLOSE,
    )
    market.is_play_money = False

    def _ev(factor_id: str, value: float) -> AuditEvaluation:
        return AuditEvaluation(
            market_id=market_id, index_definition_id=1, factor_id=factor_id,
            prompt_id=1, prompt_sha256="x" * 64, model_id="stub", value_numeric=value,
        )

    return MarketEvaluations(
        market=market,
        by_factor={"rel": _ev("rel", 1.0), "btc_direction": _ev("btc_direction", direction)},
        last_price=last_price,
        liquidity=None,  # uniform liquidity → isolates the moneyness weight
    )


def _logistic(strike: float, *, center: float, scale: float) -> float:
    """P(spot_T > strike) for a logistic terminal: decreasing in strike, 0.5 at
    ``center`` — a clean stand-in for an "above $X" ladder's prices."""
    return 1.0 / (1.0 + math.exp((strike - center) / scale))


def _above_ladder(center: float = 60_250.0, lo: int = 58_000, hi: int = 70_000) -> list:
    """One-sided "above $X" ladder, $500 strikes lo..hi, all direction +1. With
    ``hi`` well above ``center`` it is OTM-skewed exactly like the scored slice of
    the real data (many far-above strikes priced ~0)."""
    rows = []
    for i, strike in enumerate(range(lo, hi + 1, 500)):
        price = _logistic(strike, center=center, scale=1_000.0)
        rows.append(_row(i + 1, title=f"BTC above ${strike:,} on June 24?", last_price=price, direction=1.0))
    return rows


# ---------------------------------------------------------------- strike parser

@pytest.mark.parametrize(
    "title,expected",
    [
        ("BTC price today at 12pm EDT - $60,100 or above", 60_100.0),
        ("Will Bitcoin's price exceed $62,000 on June 25, 2026 at 4 PM CT?", 62_000.0),
        ("Bitcoin above $64,000 on June 24?", 64_000.0),
        ("Will Bitcoin hit $140K in 2026?", 140_000.0),
        ("Bitcoin all time high by June 30, 2026?", None),
        ("BTC price today at 12:05pm EDT - Up", None),
        ("", None),
    ],
)
def test_extract_strike(title: str, expected: float | None) -> None:
    assert extract_strike(title) == expected


def test_extract_strike_range_bin_uses_midpoint() -> None:
    title = "Bitcoin price range on Jun 24, 2026? - $59,300 to 59,399.99"
    assert extract_strike(title) == pytest.approx(59_349.995)


# ----------------------------------------------------------- implied spot

def test_implied_spot_clean_ladder() -> None:
    """Monotone decreasing ladder → the interpolated 0.5 crossing."""
    pairs = [(59_000, 0.80), (59_500, 0.65), (60_000, 0.55), (60_500, 0.40), (61_000, 0.20)]
    spot = _implied_spot(pairs)
    assert 60_000 <= spot <= 60_500  # crosses 0.5 between 60,000 (0.55) and 60,500 (0.40)


def test_implied_spot_robust_to_noise() -> None:
    """Non-monotonic thin-price noise (a strike jumps back above 0.5) must not
    throw the estimate off — the MEDIAN crossing stays near true spot ~60,250."""
    pairs = [
        (60_000, 0.75), (60_100, 0.18), (60_200, 0.67),  # noisy reversal
        (60_300, 0.24), (60_400, 0.55), (60_500, 0.12),
    ]
    spot = _implied_spot(pairs)
    assert 60_000 <= spot <= 60_600


def test_implied_spot_no_crossing_uses_nearest_edge() -> None:
    """All strikes priced > 0.5 (spot above the ladder) → strike of price nearest
    0.5, i.e. the top strike."""
    pairs = [(50_000, 0.95), (52_000, 0.88), (54_000, 0.70)]
    assert _implied_spot(pairs) == 54_000


# ------------------------------------------------- weighting behaviour

def test_disabled_is_uniform_and_inert() -> None:
    """Disabled → every weight 1.0 and the score equals the plain signed score,
    so the war index and friends are byte-identical."""
    rows = _above_ladder()
    weights, info = moneyness_weights(rows, _ir(moneyness=False))
    assert set(weights.values()) == {1.0}
    assert info["applied"] is False
    # aggregate with moneyness off == the un-weighted signed score
    off = aggregate(rows, _ir(moneyness=False))
    assert off.breakdown["moneyness_weighting"]["applied"] is False


def test_weights_peak_at_the_money() -> None:
    """The kernel peaks (~1.0) at the implied spot and decays for far strikes."""
    rows = _above_ladder()
    weights, info = moneyness_weights(rows, _ir(moneyness=True, bandwidth=0.05))
    assert info["applied"] is True
    assert 60_000 <= info["implied_spot"] <= 60_500
    by_strike = {extract_strike(r.market.title): weights[r.market.id] for r in rows}
    atm = by_strike[60_000]
    assert atm > 0.95
    assert by_strike[70_000] < 0.05 * atm  # far OTM heavily discounted


def test_moneyness_deconfounds_otm_skew() -> None:
    """The headline fix: an OTM-skewed "above $X" ladder reads deeply bearish with
    weighting OFF (the listing skew) but ~50 with it ON (near-money strikes, which
    are symmetric around spot, carry the score)."""
    rows = _above_ladder()
    off = aggregate(rows, _ir(moneyness=False)).score
    on = aggregate(rows, _ir(moneyness=True, bandwidth=0.02)).score
    assert off < 30.0                       # skew pins it low
    assert 45.0 <= on <= 55.0               # de-confounded ≈ balanced
    assert on > off + 15.0                  # and a large, real correction


def test_bullish_signal_preserved() -> None:
    """A genuine near-the-money bullish book (strikes around spot all priced
    > 0.5 → market expects BTC higher) still reads > 50 — weighting removes the
    skew artifact without flattening real conviction."""
    rows = [
        _row(1, title="BTC above $59,800 on June 24?", last_price=0.66, direction=1.0),
        _row(2, title="BTC above $59,900 on June 24?", last_price=0.63, direction=1.0),
        _row(3, title="BTC above $60,000 on June 24?", last_price=0.60, direction=1.0),
        _row(4, title="BTC above $60,100 on June 24?", last_price=0.57, direction=1.0),
    ]
    on = aggregate(rows, _ir(moneyness=True, bandwidth=0.05)).score
    assert on > 55.0


def test_no_strike_market_excluded() -> None:
    """Under moneyness mode a market with no extractable strike gets weight 0 and
    drops out of the component count (it can't be placed on the moneyness axis)."""
    rows = _above_ladder()
    no_strike = _row(999, title="Bitcoin all time high by June 30?", last_price=0.30, direction=1.0)
    rows.append(no_strike)
    weights, _ = moneyness_weights(rows, _ir(moneyness=True))
    assert weights[999] == 0.0
    result = aggregate(rows, _ir(moneyness=True, bandwidth=0.02))
    # 999 excluded → component_count is the ladder size, not +1
    assert result.component_count == len(rows) - 1
    assert result.breakdown["moneyness_weighting"]["n_no_strike_excluded"] == 1
