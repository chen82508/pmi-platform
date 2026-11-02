"""Strike-aware moneyness weighting for price-ladder indexes (BTC Bull/Bear v4).

WHY — the moneyness / sentiment confound
----------------------------------------
An "above $X" market's YES probability is ``P(spot_T > X)``: it bundles *where
spot is* (moneyness) with *directional conviction*. Under the signed formula
(``price_term = 2·price − 1``) a far-OTM strike (X ≫ spot) is correctly priced
~0 yet contributes a spurious −0.9 ("bearish"). Because a strike ladder lists
far more strikes above spot than a book ever trades below it, that structural
skew — not sentiment — dragged BTC v3 to ~16 (see the def's KNOWN RESIDUAL).

FIX — re-weight by distance from a ladder-implied spot
------------------------------------------------------
1. Read *current spot* off the ladders themselves: within one strike ladder
   (a venue + a resolution date), the "above $X" price is monotone-decreasing
   in X in expectation, so the strike where interpolated P(YES) crosses 0.5 is
   the implied median terminal price ≈ spot. No external price feed → the
   offline / $0 cache-hit path is preserved.
2. Weight every market by a Gaussian kernel in log-moneyness ``ln(strike/spot)``:
   near-the-money strikes (price genuinely encodes up/down conviction) keep full
   weight; deep OTM/ITM strikes (which only encode distance-from-spot) decay to
   ~0.

A balanced book then reads 50, and the index moves on real directional pricing —
a longer-dated near-spot strike priced > 0.5 means the market expects BTC *above
current spot* at that date (bullish). One GLOBAL spot is used (the median of the
per-ladder spots), not a per-ladder spot: centring each ladder on its own spot
would pin every ladder at 50 and erase the cross-horizon drift signal.

Thin last-trade prices are noisy and non-monotonic, so the implied spot is the
MEDIAN of all 0.5-crossings in a ladder, not the first — robust to a few jumps,
and in any case far finer than the ~$3k kernel bandwidth needs.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from pmi_core.dsl.ir import IndexDef

# First $-prefixed dollar amount in a title: "$60,100", "$140K", "$1,234.56".
# Range bins ("75,000 to 79,999.99" — often with NO $ sign in the live DB) are
# parsed as their midpoint so the band lands on the moneyness axis. Previously a
# range without a $ matched nothing and the market was dropped as "no strike".
_RANGE_RE = re.compile(
    r"\$?\s?([0-9][0-9,]{2,}(?:\.[0-9]+)?)\s*(?:to|\u2013|\u2014)\s*\$?\s?([0-9][0-9,]{2,}(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_STRIKE_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s?([kKmM])?")
_MULT = {"k": 1_000.0, "m": 1_000_000.0}


def extract_strike(title: str | None) -> float | None:
    """Parse the first dollar strike from a market title, or None if absent."""
    if not title:
        return None
    rng = _RANGE_RE.search(title)
    if rng is not None:
        try:
            lo = float(rng.group(1).replace(",", ""))
            hi = float(rng.group(2).replace(",", ""))
        except ValueError:
            lo = hi = 0.0
        if lo > 0 and hi > 0:
            return (lo + hi) / 2.0
    m = _STRIKE_RE.search(title)
    if m is None:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix:
        val *= _MULT[suffix]
    return val if val > 0 else None


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _implied_spot(strike_price: list[tuple[float, float]]) -> float | None:
    """Implied spot for one ladder = MEDIAN strike at which the (strike-sorted)
    price series crosses 0.5. Robust to non-monotonic last-trade noise.

    Falls back to the strike of the price closest to 0.5 when the series never
    crosses (spot lies outside the listed strikes). None for an empty ladder.
    """
    if not strike_price:
        return None
    pts = sorted(strike_price)
    crossings: list[float] = []
    for (x0, p0), (x1, p1) in zip(pts, pts[1:], strict=False):
        if (p0 - 0.5) * (p1 - 0.5) < 0:  # strictly brackets 0.5
            crossings.append(x0 + (0.5 - p0) * (x1 - x0) / (p1 - p0))
        elif p0 == 0.5:
            crossings.append(x0)
    if crossings:
        return _median(crossings)
    # No crossing: every strike is same-side of 0.5 → spot is past an edge.
    return min(pts, key=lambda t: abs(t[1] - 0.5))[0]


def moneyness_weights(rows, ir: IndexDef) -> tuple[dict[int, float], dict]:
    """Return ``({market_id: weight}, breakdown)`` per ``weighting.moneyness``.

    Disabled (the default) → every market weight 1.0, so the aggregator is
    byte-identical to the pre-v4 behaviour. ``rows`` are ``MarketEvaluations``
    (post-collapse); each exposes ``.market`` (title, venue, closes_at),
    ``.last_price`` and ``.by_factor``.
    """
    spec = getattr(ir.weighting, "moneyness", None)
    info: dict = {"applied": False}
    if spec is None or not spec.enabled:
        info["reason"] = "disabled"
        return {r.market.id: 1.0 for r in rows}, info

    # The ternary factor (located by TYPE, like the aggregator) flags "above $X"
    # markets whose price is monotone in strike — the only ones that vote on spot.
    direction_id = next((f.id for f in ir.factors if f.type == "ternary"), None)

    ladders: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    strike_by_market: dict[int, float] = {}
    n_no_strike = 0
    for r in rows:
        strike = extract_strike(r.market.title)
        if strike is None:
            n_no_strike += 1
            continue
        strike_by_market[r.market.id] = strike
        if r.last_price is None:
            continue
        direction = None
        if direction_id is not None:
            e = r.by_factor.get(direction_id)
            direction = e.value_numeric if e is not None else None
        if direction == 1.0:  # YES = price up → "above $X" ladder member
            close_key = r.market.closes_at.date() if r.market.closes_at else None
            ladders[(r.market.venue, close_key)].append((strike, float(r.last_price)))

    # Per-ladder spot, then global spot = median of them. Near-dated ladders all
    # imply ~current spot, so the median is a robust, drift-insensitive estimate;
    # tiny ladders (< min_ladder_size) don't vote but their markets are still
    # weighted by the global spot below.
    ladder_spots: list[float] = []
    for sp in ladders.values():
        if len(sp) < spec.min_ladder_size:
            continue
        s = _implied_spot(sp)
        if s is not None and s > 0:
            ladder_spots.append(s)

    if not ladder_spots:
        info["reason"] = "no ladder ≥ min_ladder_size; weighting fails open (uniform)"
        info["n_no_strike"] = n_no_strike
        return {r.market.id: 1.0 for r in rows}, info

    spot = _median(ladder_spots)
    b = spec.bandwidth
    two_b2 = 2.0 * b * b

    weights: dict[int, float] = {}
    for r in rows:
        strike = strike_by_market.get(r.market.id)
        if strike is None:
            # No strike → can't place on the moneyness axis. Exclude (weight 0)
            # rather than let it ride at full weight while laddered strikes are
            # kernel-discounted; the aggregator drops zero-weight markets.
            weights[r.market.id] = 0.0
            continue
        lm = math.log(strike / spot)
        weights[r.market.id] = math.exp(-(lm * lm) / two_b2)

    # Kish effective sample size (Σw)²/Σw² — how many near-money markets actually
    # carry the score after kernel discounting, surfaced for the breakdown.
    wv = [w for w in weights.values() if w > 0]
    sw = sum(wv)
    sw2 = sum(w * w for w in wv)
    info.update(
        {
            "applied": True,
            "implied_spot": round(spot, 2),
            "bandwidth": b,
            "n_ladders_voting": len(ladder_spots),
            "ladder_spots": [round(s, 2) for s in sorted(ladder_spots)],
            "n_no_strike_excluded": n_no_strike,
            "effective_components": round(sw * sw / sw2, 1) if sw2 > 0 else 0,
        }
    )
    return weights, info
