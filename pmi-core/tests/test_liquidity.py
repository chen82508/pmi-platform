"""Unit tests for the shared liquidity normalization (engine/liquidity.py)."""

from __future__ import annotations

from pmi_core.engine.liquidity import effective_liquidity, has_liquidity


def test_effective_liquidity_collapses_null_zero_negative_to_zero() -> None:
    assert effective_liquidity(None) == 0.0     # no-data
    assert effective_liquidity(0.0) == 0.0      # untraded
    assert effective_liquidity(-5.0) == 0.0     # defensive: never negative
    assert effective_liquidity(1.0) == 1.0
    assert effective_liquidity(1234.5) == 1234.5


def test_has_liquidity_only_positive_participates() -> None:
    assert has_liquidity(10.0) is True
    assert has_liquidity(None) is False   # no-data → weight 0, member not scored
    assert has_liquidity(0.0) is False    # untraded → same
    assert has_liquidity(-1.0) is False
