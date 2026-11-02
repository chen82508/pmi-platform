"""Unit tests for the ``seat_projection_sum`` aggregator branch.

Pure-Python, no DB / MLflow / network — builds ``MarketEvaluations`` with stub
``AuditEvaluation`` rows and asserts on the seat-projection score + breakdown.

What we lock in:

1. **Dispatch** — an index whose ``formula`` is ``seat_projection_sum`` routes
   to the Poisson-binomial path; the score is E[R seats] (a seat count), not a
   0..100 index, and ``breakdown["formula"]`` records the branch.
2. **Polarity collapse** — the score reuses ``extract_contested_seats``, so a
   "Democrats win <state>" market is read as ``1 - price`` and same-state R/D
   markets dedup to one seat (prefer the R market).
3. **Holdover geometry** — E[R seats] = holdover_r + Σ P(R wins) over contested
   seats, matching ``compute_seat_distribution``.
4. **Honest degradation** — markets that aren't per-state senate races (e.g.
   House districts) match no seats → ``score=None`` below ``min_components``.
"""

from __future__ import annotations

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
    LiquidityWeighting,
    SeatProjectionSpec,
    WeightingSpec,
)
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.models import AuditEvaluation, CoreMarket


def _ir(
    *,
    min_components: int = 1,
    seat_projection: SeatProjectionSpec | None = None,
) -> IndexDef:
    return IndexDef(
        id="senate-seats-test",
        version=1,
        title="Senate Seats Test",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["senate"])],
        factors=[
            FactorSpec(
                id="is_senate_race_2026",
                type="binary",
                prompt_ref="prompts/factors/is-senate-race-2026-v1",
                weight=60,
            ),
            FactorSpec(
                id="republican_on_yes",
                type="binary",
                prompt_ref="prompts/factors/republican-on-yes-v1",
                weight=40,
            ),
        ],
        aggregation=AggregationSpec(
            collapse=CollapseSpec(enabled=True),
            min_components=min_components,
            formula="seat_projection_sum",
            seat_projection=seat_projection,
        ),
        weighting=WeightingSpec(liquidity=LiquidityWeighting(method="none")),
    )


def _row(
    market_id: int,
    title: str,
    last_price: float,
    *,
    raw: dict | None = None,
    republican_on_yes: float | None = None,
    liquidity: float | None = 1000.0,
) -> MarketEvaluations:
    market = CoreMarket(
        id=market_id,
        venue="polymarket",
        external_id=f"ext-{market_id}",
        slug=None,
        title=title,
        raw=raw,
    )
    # One gating eval, just so component lineage has something to collect.
    by_factor = {
        "is_senate_race_2026": AuditEvaluation(
            id=market_id * 10,
            market_id=market_id,
            index_definition_id=1,
            factor_id="is_senate_race_2026",
            prompt_id=1,
            prompt_sha256="x" * 64,
            model_id="stub",
            value_numeric=1.0,
        )
    }
    if republican_on_yes is not None:
        by_factor["republican_on_yes"] = AuditEvaluation(
            id=market_id * 10 + 1,
            market_id=market_id,
            index_definition_id=1,
            factor_id="republican_on_yes",
            prompt_id=1,
            prompt_sha256="x" * 64,
            model_id="stub",
            value_numeric=republican_on_yes,
        )
    # Positive liquidity by default so the race reads as TRADED: the seat
    # projection uses the market price. Pass liquidity=None to exercise the
    # §F(a) zero-liquidity path (falls back to the incumbent-hold prior).
    return MarketEvaluations(
        market=market,
        by_factor=by_factor,
        last_price=last_price,
        liquidity=liquidity,
    )


# --------------------------------------------------------------------------
# Dispatch + expected seat count
# --------------------------------------------------------------------------


def test_score_is_expected_r_seats_with_holdover() -> None:
    rows = [
        _row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.80),
        _row(2, "Will the Republicans win the Texas Senate race in 2026?", 0.60),
    ]
    sp = SeatProjectionSpec(
        total_seats=100, majority_threshold=51, holdover_r=30, holdover_d=37
    )
    result = aggregate(rows, _ir(seat_projection=sp))

    # E[R] = holdover_r + 0.80 + 0.60 = 31.4
    assert result.score == 31.4
    assert result.component_count == 2
    assert result.breakdown["formula"] == "seat_projection_sum"
    assert result.breakdown["expected_r_seats"] == 31.4
    assert result.breakdown["n_contested"] == 2
    assert result.breakdown["holdover_r"] == 30
    # lineage flows through for the seat markets
    assert sorted(result.component_evaluation_ids) == [10, 20]


def test_democrat_market_counted_as_one_minus_price() -> None:
    # A "Democrats win" market collapses to P(R) = 1 - price.
    rows = [
        _row(1, "Will the Democrats win the Maine Senate race in 2026?", 0.70),
    ]
    result = aggregate(rows, _ir(seat_projection=SeatProjectionSpec()))
    # P(R) = 1 - 0.70 = 0.30; no holdover → E[R] = 0.30
    assert result.score == 0.30
    assert result.breakdown["n_contested"] == 1


def test_same_state_r_and_d_collapse_to_one_seat() -> None:
    rows = [
        _row(1, "Will the Republicans win the Georgia Senate race in 2026?", 0.55),
        _row(2, "Will the Democrats win the Georgia Senate race in 2026?", 0.45),
    ]
    result = aggregate(rows, _ir(seat_projection=SeatProjectionSpec()))
    # One Georgia seat, R market preferred → P(R) = 0.55
    assert result.breakdown["n_contested"] == 1
    assert result.score == 0.55


def test_default_geometry_when_seat_projection_omitted() -> None:
    rows = [_row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.90)]
    result = aggregate(rows, _ir(seat_projection=None))
    assert result.score == 0.90
    assert result.breakdown["total_seats"] == 100
    assert result.breakdown["holdover_r"] == 0


# --------------------------------------------------------------------------
# Honest degradation
# --------------------------------------------------------------------------


def test_non_senate_markets_yield_no_seats() -> None:
    # House-district / unrelated titles aren't matched by parse_seat_race.
    rows = [
        _row(1, "Will the Republicans win the 2026 House majority?", 0.60),
        _row(2, "Who will be the Republican nominee in Ohio?", 0.30),
    ]
    result = aggregate(rows, _ir(min_components=1, seat_projection=SeatProjectionSpec()))
    assert result.score is None
    assert result.component_count == 0
    assert result.breakdown["reason"] == "below min_components"
    assert result.breakdown["contested_seats"] == 0


def test_below_min_components_returns_none() -> None:
    rows = [_row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.80)]
    result = aggregate(rows, _ir(min_components=5, seat_projection=SeatProjectionSpec()))
    assert result.score is None
    assert result.breakdown["contested_seats"] == 1


# --------------------------------------------------------------------------
# Candidate-name markets (the Alaska gap) — party hints from raw / factor
# --------------------------------------------------------------------------


def test_candidate_market_mapped_via_republican_on_yes_factor() -> None:
    # Alaska 2026: no party-direct market; groupItemTitle is a bare name, so
    # the audited republican_on_yes=1.0 eval is the party signal.
    rows = [
        _row(
            38481,
            "Will Dan Sullivan win the Alaska Senate race in 2026?",
            0.375,
            raw={"groupItemTitle": "Dan Sullivan"},
            republican_on_yes=1.0,
        ),
        _row(
            38479,
            "Will Mary Peltola win the Alaska Senate race in 2026?",
            0.55,
            raw={"groupItemTitle": "Mary Peltola"},
            republican_on_yes=0.0,
        ),
    ]
    result = aggregate(rows, _ir(seat_projection=SeatProjectionSpec()))
    # One Alaska seat; Sullivan (R) price wins, Peltola (0.0 ≠ "D") is skipped
    # as a seat source but harmless.
    assert result.breakdown["n_contested"] == 1
    assert result.score == 0.375


def test_candidate_market_mapped_via_group_item_title_tag() -> None:
    rows = [
        _row(
            7,
            "Will Jane Doe win the Maine Senate race in 2026?",
            0.30,
            raw={"groupItemTitle": "Jane Doe (D)"},
        ),
    ]
    result = aggregate(rows, _ir(seat_projection=SeatProjectionSpec()))
    # D candidate → P(R) = 1 - 0.30
    assert result.breakdown["n_contested"] == 1
    assert result.score == 0.70


def test_candidate_market_without_party_signal_stays_out() -> None:
    rows = [
        _row(
            8,
            "Will Richard Grayson win the Alaska Senate race in 2026?",
            0.01,
            raw={"groupItemTitle": "Richard Grayson"},
            republican_on_yes=0.0,
        ),
    ]
    result = aggregate(rows, _ir(min_components=1, seat_projection=SeatProjectionSpec()))
    assert result.score is None
    assert result.breakdown["contested_seats"] == 0


def test_candidate_seat_lineage_flows_through() -> None:
    rows = [
        _row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.80),
        _row(
            2,
            "Will Dan Sullivan win the Alaska Senate race in 2026?",
            0.40,
            raw={"groupItemTitle": "Dan Sullivan"},
            republican_on_yes=1.0,
        ),
    ]
    sp = SeatProjectionSpec(
        total_seats=100, majority_threshold=51, holdover_r=31, holdover_d=34
    )
    result = aggregate(rows, _ir(seat_projection=sp))
    assert result.breakdown["n_contested"] == 2
    # E[R] = 31 + 0.80 + 0.40
    assert result.score == 32.2
    # Both seats' evaluations (incl. the Alaska market's two factors) in lineage.
    assert sorted(result.component_evaluation_ids) == [10, 20, 21]


# --------------------------------------------------------------------------
# §F(a) refined — zero-liquidity prior (only running incumbents get the hold)
# --------------------------------------------------------------------------


def test_zero_liquidity_pr_branches() -> None:
    from pmi_core.engine.aggregator import _zero_liquidity_pr

    # GROSS misread — thin gives the known running incumbent's party < 25% → the
    # incumbent-hold overrides it (keyed by state for senate, per-district for house).
    assert _zero_liquidity_pr(0.10, "WV", "senate", 2026) == 1.0    # WV (R), R only 10%
    assert _zero_liquidity_pr(0.90, "NM", "senate", 2026) == 0.0    # NM (D), D only 10%
    assert _zero_liquidity_pr(0.10, "AR-1", "house", 2026) == 1.0   # AR-1 (R) gross misread
    assert _zero_liquidity_pr(0.90, "CA-43", "house", 2026) == 0.0  # CA-43 (D) gross misread
    # CONSISTENT thin (incumbent's party ≥ 25%) → KEEP the thin read (competitiveness),
    # do NOT snap a safe/competitive incumbent to certainty.
    assert _zero_liquidity_pr(0.63, "NV-2", "house", 2026) == 0.63  # NV-2 (R), R 63% → keep
    assert _zero_liquidity_pr(0.55, "WV", "senate", 2026) == 0.55   # WV (R), R 55% → keep
    assert _zero_liquidity_pr(0.92, "AR-1", "house", 2026) == 0.92  # safe R read → keep, not 1.0
    # OPEN seat (incumbent not running) → keep the thin read even if it looks extreme.
    assert _zero_liquidity_pr(0.65, "KS", "governor", 2026) == 0.65  # KS termed out
    assert _zero_liquidity_pr(0.05, "AK", "governor", 2026) == 0.05  # AK termed out, extreme → keep
    # Unlisted district / no office → keep the thin read, no flatten.
    assert _zero_liquidity_pr(0.40, "ZZ-9", "house", 2026) == 0.40
    assert _zero_liquidity_pr(0.40, "TX", None, 2026) == 0.40


def test_zero_liquidity_gross_misread_overrides_else_keeps_thin() -> None:
    # All three zero-liquidity (liquidity=None).
    rows = [
        # Ohio: roster R, running, thin gives R only 10% → GROSS misread → hold 1.0.
        _row(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.10, liquidity=None),
        # Idaho: roster R, running, thin 0.60 is consistent with R → KEEP thin.
        _row(2, "Will the Republicans win the Idaho Senate race in 2026?", 0.60, liquidity=None),
        # Texas: OPEN (Cornyn lost the primary) → keep thin regardless.
        _row(3, "Will the Republicans win the Texas Senate race in 2026?", 0.45, liquidity=None),
    ]
    result = aggregate(rows, _ir(seat_projection=SeatProjectionSpec()))
    # OH 1.0 (override) + ID 0.60 (kept) + TX 0.45 (open, kept) = 2.05
    assert result.score == 2.05
    assert result.breakdown["n_contested"] == 3


def test_zero_liquidity_rescue_requires_single_venue() -> None:
    # CA-01 (2026-10-12): six real-money venues quoted 6–13 R on a $0-volume
    # day; the roster still lists its pre-redistricting Republican holder, so
    # the single-venue rule would have called it a gross misread → 100.
    from pmi_core.engine.aggregator import _zero_liquidity_pr

    assert _zero_liquidity_pr(0.07, "AR-1", "house", 2026, n_real_venues=6) == 0.07
    assert _zero_liquidity_pr(0.07, "AR-1", "house", 2026, n_real_venues=2) == 0.07
    assert _zero_liquidity_pr(0.07, "AR-1", "house", 2026, n_real_venues=1) == 1.0
    assert _zero_liquidity_pr(0.07, "AR-1", "house", 2026) == 1.0  # default = single venue
    assert _zero_liquidity_pr(0.10, "WV", "senate", 2026, n_real_venues=3) == 0.10
