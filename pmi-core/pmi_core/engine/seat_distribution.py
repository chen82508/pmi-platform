"""Balance-of-power seat distribution (CORR-1.6).

Drives the senate-board UI's top-line probabilities. Given the per-race
market-implied probability that Republicans *win each contested seat*, plus
the holdover seats not on the ballot, this module computes:

  * ``p_r_majority`` = P(total R seats >= threshold)   → board ``pmiGOPMajority``
  * ``p_d_majority`` = P(total D seats >= threshold)   → board ``pmiDemMajority``
  * ``expected_r_seats`` / ``stdev_r_seats``           → board E[seats] tile
  * ``total_seats_pmf``                                → seat-count histogram

The crucial correctness point this fixes (vs the senate PoC
``senate_2026_pmi.py::_aggregate_pmi``, which liquidity-weight-*averages*
direct/BoP markets): the chamber-control probability is **not** an average
or a sum of per-race probabilities. Each contested race is an independent
Bernoulli(p_i); the number of R wins among them follows a
**Poisson-binomial** distribution. Its exact PMF is the convolution of the
n per-race ``[1 - p_i, p_i]`` two-point distributions, which we evaluate with
``numpy.convolve``; we then shift it by the holdover R seats and read tail
mass for the majority thresholds. (We use numpy rather than scipy because
scipy has no *exact* Poisson-binomial — only normal approximations, which we
don't need at n~35.)

Independence across races is an approximation (national swing correlates
races), the same approximation the noisy-OR bucket collapser already makes.
A correlated model is future work; this is the honest first-order answer and
is exact *given* independence.

Pure-Python, no DB / LLM / network — unit-testable in isolation.

Band classification (``classify_band`` / ``band_counts``) mirrors the
frontend ``pmi-new-frontend/data-senate.js`` ``bandOf`` so the board's
7-band seat tally is computed once, server-side. Cutoffs here are in
probability space [0, 1]; the frontend expresses them as 0..100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

# Gauss-Hermite nodes for the national-swing integral in the correlated model.
# 48 is far more than the smooth 1-D integrand needs (error ~1e-12) but costs
# only 48 convolutions of n~35 — negligible.
_GH_NODES = 48
_NORMAL = NormalDist()

# --- Band classification (mirror of frontend data-senate.js `bandOf`) ------
# Keys and order are load-bearing: the board renders bands left→right Dem→Rep.
BANDS: tuple[str, ...] = (
    "safe-d",
    "likely-d",
    "lean-d",
    "tossup",
    "lean-r",
    "likely-r",
    "safe-r",
)

# Upper-inclusive cutoffs in P(R wins) space. Checked in ascending order;
# first match wins. Mirrors bandOf: <=0.10 safe-d, <=0.25 likely-d,
# <=0.40 lean-d, <0.60 tossup, <0.75 lean-r, <0.90 likely-r, else safe-r.
_BAND_CUTOFFS: tuple[tuple[float, str], ...] = (
    (0.10, "safe-d"),
    (0.25, "likely-d"),
    (0.40, "lean-d"),
    (0.60, "tossup"),   # 0.40 < p < 0.60
    (0.75, "lean-r"),
    (0.90, "likely-r"),
)


def classify_band(p_r_win: float) -> str:
    """Map a single race's P(R wins) in [0, 1] to one of the 7 bands.

    The tossup / lean cutoffs are half-open on the upper side to match the
    frontend (a 0.40 race is lean-d, a 0.60 race is lean-r).
    """
    p = _clamp(p_r_win)
    # safe-d / likely-d / lean-d are upper-inclusive; tossup/lean-r/likely-r
    # are upper-exclusive. The lean-d cutoff (<=0.40) and tossup lower bound
    # (>0.40) are handled by ordering: a value of exactly 0.40 matches lean-d.
    for cutoff, band in _BAND_CUTOFFS:
        if band in ("safe-d", "likely-d", "lean-d"):
            if p <= cutoff:
                return band
        elif p < cutoff:
            return band
    return "safe-r"


def band_counts(
    contested_probs: list[float],
    *,
    holdover_r: int = 0,
    holdover_d: int = 0,
) -> dict[str, int]:
    """Count contested races per band, then fold holdover seats into the safes.

    Mirrors the frontend's ``counts`` object: every contested race lands in
    exactly one band by ``classify_band``; holdover (not-on-ballot) R seats
    add to ``safe-r`` and holdover D seats add to ``safe-d`` — they are, by
    definition, locked.
    """
    counts = dict.fromkeys(BANDS, 0)
    for p in contested_probs:
        counts[classify_band(p)] += 1
    counts["safe-d"] += holdover_d
    counts["safe-r"] += holdover_r
    return counts


# --- Poisson-binomial seat distribution ------------------------------------


@dataclass(frozen=True)
class SeatDistribution:
    """Result of folding per-race Bernoulli outcomes into a chamber view.

    All probabilities are in [0, 1]; the API layer (SHIP-2.5) multiplies by
    100 for the board's ``pmiGOPMajority`` / ``pmiDemMajority`` fields.
    """

    expected_r_seats: float
    stdev_r_seats: float
    p_r_majority: float            # P(total R seats >= threshold)
    p_d_majority: float            # P(total D seats >= threshold)
    # Chamber *control*: same tails, but the tie-break party (the VP's party
    # for the US Senate) controls at threshold - 1 seats. With no tie_break
    # these equal the majority probabilities.
    p_r_control: float
    p_d_control: float
    tie_break: str | None
    contested_pmf: list[float]     # pmf[k] = P(exactly k R wins among contested)
    total_seats_pmf: dict[int, float]  # total R seats (incl holdover) -> prob
    n_contested: int
    holdover_r: int
    holdover_d: int
    total_seats: int
    majority_threshold: int


def _clamp(p: float) -> float:
    return max(0.0, min(1.0, float(p)))


def _poisson_binomial_pmf(probs: list[float]) -> list[float]:
    """Exact PMF of the number of successes across independent Bernoulli(p_i).

    The Poisson-binomial PMF is the convolution of the n per-race two-point
    distributions ``[1 - p_i, p_i]``; we fold them with ``np.convolve``.
    Returns ``pmf`` of length ``len(probs) + 1`` where ``pmf[k]`` is
    P(exactly k successes). Empty input → ``[1.0]`` (0 successes w.p. 1).
    """
    pmf = np.array([1.0])
    for p in probs:
        p = _clamp(p)
        pmf = np.convolve(pmf, [1.0 - p, p])
    return pmf.tolist()


def _correlated_pmf(probs: list[float], correlation: float) -> list[float]:
    """PMF of R wins under a single-factor Gaussian-copula swing model.

    Each race wins for R iff a latent ``X_i = √ρ·S + √(1−ρ)·ε_i`` — with a
    *shared* national swing ``S ~ N(0,1)`` and idiosyncratic ``ε_i ~ N(0,1)`` —
    falls at-or-below ``Φ⁻¹(p_i)``. This construction *exactly preserves every
    marginal* ``P(R wins i) = p_i`` (since ``E_S[Φ((Φ⁻¹(p_i) − √ρ·S)/√(1−ρ))]
    = p_i``) while coupling the races through ``S`` with pairwise correlation
    ``ρ``. Conditional on ``S = s`` the races are independent, so the
    conditional win count is an exact Poisson-binomial (we reuse
    :func:`_poisson_binomial_pmf`); we then integrate over ``s`` with
    Gauss-Hermite quadrature.

    ``ρ = 0`` reproduces the independent Poisson-binomial (up to quadrature
    error); larger ρ fattens the tails — the point of the model, since
    national swing makes Senate races move together and the independent model
    understates the chance either party runs the table on the close races.
    """
    rho = min(max(float(correlation), 0.0), 1.0 - 1e-9)
    n = len(probs)
    if n == 0:
        return [1.0]
    if rho == 0.0:
        return _poisson_binomial_pmf(probs)

    eps = 1e-12
    thresholds = [_NORMAL.inv_cdf(min(max(p, eps), 1.0 - eps)) for p in probs]
    sqrt_rho = math.sqrt(rho)
    sqrt_comp = math.sqrt(1.0 - rho)

    # Probabilist's Gauss-Hermite (weight e^{−s²/2}); for S ~ N(0,1),
    # E_S[f] = (1/√(2π)) Σ_j w_j f(s_j).
    nodes, weights = np.polynomial.hermite_e.hermegauss(_GH_NODES)
    norm = 1.0 / math.sqrt(2.0 * math.pi)

    mix = np.zeros(n + 1)
    for s, w in zip(nodes, weights, strict=False):
        cond = [_NORMAL.cdf((t - sqrt_rho * s) / sqrt_comp) for t in thresholds]
        mix += (norm * w) * np.asarray(_poisson_binomial_pmf(cond))
    return mix.tolist()


def compute_seat_distribution(
    contested_probs: list[float],
    *,
    holdover_r: int = 0,
    holdover_d: int = 0,
    total_seats: int = 100,
    majority_threshold: int = 51,
    tie_break: str | None = None,
    correlation: float | None = None,
) -> SeatDistribution:
    """Build the chamber-control distribution from per-race R-win probabilities.

    Parameters
    ----------
    contested_probs:
        P(Republican wins) for each seat on the ballot, in [0, 1].
    holdover_r / holdover_d:
        Seats not on the ballot, already held by each party (constants added
        to every realization). For the 2026 Senate: 100 = 33 contested +
        67 holdover, so e.g. holdover_r=32, holdover_d=34, leaving 34 (or 33)
        contested depending on specials.
    total_seats:
        Chamber size (100 for Senate, 435 for House). Metadata only — the D
        seat count is ``holdover_d + (n_contested - r_wins)``, NOT
        ``total_seats - r_seats``, so seats missing from the modeled universe
        (no party-direct market) count toward neither party.
    majority_threshold:
        Seats needed for an *outright* majority (51 for a 100-seat Senate).
    tie_break:
        Which party controls a tied chamber — "R" or "D" (the VP's party for
        the US Senate), or None for chambers with no tie-break. The tie-break
        party's *control* threshold is ``majority_threshold - 1``.
    correlation:
        Pairwise national-swing correlation ρ ∈ [0, 1) across the contested
        races (single-factor Gaussian copula, :func:`_correlated_pmf`).
        ``None`` or ``0`` → the exact independent Poisson-binomial (this path
        is byte-identical to the pre-correlation engine). ρ > 0 preserves
        every marginal but fattens the seat-count tails, which is what makes
        the chamber-control tails agree with a direct control market; fit ρ
        with :func:`calibrate_correlation`.

    Returns a :class:`SeatDistribution`. ``p_r_majority + p_d_majority`` need
    not equal 1: with an even chamber and threshold = size/2 + 1 there is
    tie mass (e.g. 50-50) belonging to neither. ``p_r_control + p_d_control``
    DOES equal 1 when a tie_break is set and the modeled universe covers the
    whole chamber (every seat is someone's).
    """
    probs = [_clamp(p) for p in contested_probs]
    rho = None if correlation is None else min(max(float(correlation), 0.0), 1.0 - 1e-9)
    contested_pmf = (
        _correlated_pmf(probs, rho) if rho else _poisson_binomial_pmf(probs)
    )

    # Control thresholds: the tie-break party also controls a tied chamber.
    r_control_threshold = majority_threshold - (1 if tie_break == "R" else 0)
    d_control_threshold = majority_threshold - (1 if tie_break == "D" else 0)

    # Shift contested wins by holdover to get total R seats; build the pmf.
    total_seats_pmf: dict[int, float] = {}
    p_r_majority = 0.0
    p_r_control = 0.0
    for k, mass in enumerate(contested_pmf):
        if mass == 0.0:
            continue
        r_seats = k + holdover_r
        total_seats_pmf[r_seats] = total_seats_pmf.get(r_seats, 0.0) + mass
        if r_seats >= majority_threshold:
            p_r_majority += mass
        if r_seats >= r_control_threshold:
            p_r_control += mass

    # D majority: each contested race is two-party, so D wins the n - k races
    # R loses, on top of holdover_d. Deliberately NOT total_seats - r_seats:
    # when the modeled universe is short of total_seats (e.g. a contested race
    # with no party-direct market, like Alaska 2026), the unmodeled seats must
    # count toward NEITHER party — the old subtraction silently awarded every
    # missing seat to D, inflating p_d_majority by the P(R = threshold - 1)
    # mass per missing seat.
    p_d_majority = 0.0
    p_d_control = 0.0
    for k, mass in enumerate(contested_pmf):
        if mass == 0.0:
            continue
        d_seats = holdover_d + (len(probs) - k)
        if d_seats >= majority_threshold:
            p_d_majority += mass
        if d_seats >= d_control_threshold:
            p_d_control += mass

    # Expectation is linear and the copula preserves every marginal, so
    # E[R] = holdover_r + Σ p_i regardless of ρ. The variance, however, grows
    # with ρ (the races co-move): Σ p_i(1-p_i) only holds when independent, so
    # read it off the correlated pmf otherwise.
    p_arr = np.array(probs, dtype=float)
    contested_mean = float(p_arr.sum())
    expected_r_seats = holdover_r + contested_mean
    if rho:
        variance = float(
            sum(m * (k - contested_mean) ** 2 for k, m in enumerate(contested_pmf))
        )
    else:
        variance = float((p_arr * (1.0 - p_arr)).sum())
    stdev_r_seats = math.sqrt(variance) if variance > 0 else 0.0

    return SeatDistribution(
        expected_r_seats=expected_r_seats,
        stdev_r_seats=stdev_r_seats,
        p_r_majority=p_r_majority,
        p_d_majority=p_d_majority,
        p_r_control=p_r_control,
        p_d_control=p_d_control,
        tie_break=tie_break,
        contested_pmf=contested_pmf,
        total_seats_pmf=total_seats_pmf,
        n_contested=len(probs),
        holdover_r=holdover_r,
        holdover_d=holdover_d,
        total_seats=total_seats,
        majority_threshold=majority_threshold,
    )


def calibrate_correlation(
    contested_probs: list[float],
    target_control: float,
    *,
    party: str = "R",
    holdover_r: int = 0,
    holdover_d: int = 0,
    total_seats: int = 100,
    majority_threshold: int = 51,
    tie_break: str | None = None,
    tol: float = 1e-4,
    max_iter: int = 80,
) -> float | None:
    """Fit the swing correlation ρ so model ``P(<party> control) ==
    target_control``.

    ``target_control`` is normally taken from a *direct* chamber-control
    market — e.g. the de-vigged ForecastEx "Will the Republicans win a Senate
    majority?" YES price — turning the per-race markets into a decomposition
    of a known aggregate rather than an independent re-derivation of it.

    The control tail sits above the seat-count mean (the side with E[seats]
    below its control threshold is the one with non-trivial uncertainty), so
    P(party control) is monotone increasing in ρ and a bisection converges.
    Returns ρ ∈ [0, 1), or ``None`` when the target is unreachable by
    correlation alone — i.e. at-or-below the independent (ρ=0) value, which
    widening the tails can only move further from — so the caller can leave
    the index uncorrelated rather than silently clamp.
    """

    def control(rho: float | None) -> float:
        d = compute_seat_distribution(
            contested_probs,
            holdover_r=holdover_r,
            holdover_d=holdover_d,
            total_seats=total_seats,
            majority_threshold=majority_threshold,
            tie_break=tie_break,
            correlation=rho,
        )
        return d.p_r_control if party.upper() == "R" else d.p_d_control

    base = control(0.0)
    if target_control <= base + tol:
        # Already at/above target with no correlation — nothing to fit.
        return 0.0 if abs(target_control - base) <= tol else None
    hi = 1.0 - 1e-6
    if control(hi) < target_control:
        return None  # even perfect correlation can't reach the target

    lo = 0.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if control(mid) < target_control:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)
