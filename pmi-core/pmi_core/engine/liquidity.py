"""Shared liquidity-signal normalization — the ONE place a market's raw
liquidity signal (orderbook depth, else 24h volume) collapses to a numeric
weight input, so every vertical (old and new) treats it identically.

Three states, one rule:

    TRADED   (signal > 0)      → participates in the weight ladder
    UNTRADED (signal == 0)     → weight 0: a member, shown, but does not score
    NODATA   (signal is None)  → weight 0: same as untraded

The distinction between UNTRADED and NODATA matters for *membership* semantics
(a venue that never reports volume, e.g. IBKR/gemini/predictit, is NODATA and
must still be shown), but for the *weighting math* both are 0 — a market with no
observable liquidity should not move a liquidity-weighted score. When a whole
book carries no positive signal the caller falls back to a uniform mean so the
score stays defined (see aggregator `_liquidity_weights`).

This replaces three inconsistent inline treatments (gate excluded NULL,
`_liquidity_weights` gave NULL/0 full weight 1.0, `_liquidity_by_market` gave
them 0) with a single normalization. See engine/aggregator.py.
"""

from __future__ import annotations


def effective_liquidity(signal: float | None) -> float:
    """Raw signal → its weight input. NULL / negative / 0 → 0.0; positive → value.

    The single collapse point for the three-state rule above. `0.0` means "does
    not participate in the weight ladder" (untraded or no-data alike)."""
    return float(signal) if signal is not None and signal > 0 else 0.0


def has_liquidity(signal: float | None) -> bool:
    """True iff the signal participates in weighting (i.e. is strictly positive).

    NULL (no-data) and 0 (untraded) both return False — they stay members at
    weight 0, shown but not scored."""
    return effective_liquidity(signal) > 0.0
