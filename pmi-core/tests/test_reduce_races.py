"""Unit tests for seat_mapping.reduce_races — the Stage C cross-venue reduction.

Pins the two-level model: within a venue (party-direct mean vs candidate
sum-by-party), then liquidity-weighted across venues. The TX-2 case is the bug
that motivated the rewrite.
"""

from __future__ import annotations

import pytest

from pmi_core.engine.seat_mapping import EntityObservation, reduce_races


def _obs(mid, key, party, price, tier, code=None):
    return EntityObservation(
        market_id=mid, key=key, code=code or key, party=party, price=price,
        tier=tier, venue_rank=0,
    )


def test_multi_candidate_sum_by_party_tx2() -> None:
    # Real TX-2 forecastex candidate legs: two R (Toth, Crenshaw) + one D (Finnie).
    # Old flat mean of prob_r = (0.16 + 0.10 + (1-0.12)) / 3 = 0.38 (wrong, blue).
    # reduce: ΣR = 0.26, ΣD = 0.12 → P(R) = 0.26/0.38 = 0.684 (right, red lean).
    obs = [
        _obs(1, "Texas-2", "R", 0.16, "candidates"),
        _obs(2, "Texas-2", "R", 0.10, "candidates"),
        _obs(3, "Texas-2", "D", 0.12, "candidates"),
    ]
    r = reduce_races(obs, {1: "forecastex", 2: "forecastex", 3: "forecastex"}, {})
    assert r["Texas-2"].p_r == pytest.approx(0.26 / 0.38)
    assert set(r["Texas-2"].market_ids) == {1, 2, 3}


def test_party_direct_avg_r_and_1_minus_d() -> None:
    # kalshi party-aggregate legs: R@0.60, D@0.35 → avg(0.60, 1-0.35) = 0.625.
    obs = [
        _obs(1, "Ohio", "R", 0.60, "patterns"),
        _obs(2, "Ohio", "D", 0.35, "patterns"),
    ]
    r = reduce_races(obs, {1: "kalshi", 2: "kalshi"}, {1: 100.0, 2: 100.0})
    assert r["Ohio"].p_r == pytest.approx(0.625)


def test_cross_venue_liquidity_weighted() -> None:
    # kalshi party-direct P(R)=0.625 (weight 600) + forecastex candidate P(R)=0.684
    # (weight 300) → (0.625*600 + 0.684...*300) / 900.
    obs = [
        _obs(1, "Texas", "R", 0.60, "patterns"),
        _obs(2, "Texas", "D", 0.35, "patterns"),
        _obs(3, "Texas", "R", 0.16, "candidates"),
        _obs(4, "Texas", "R", 0.10, "candidates"),
        _obs(5, "Texas", "D", 0.12, "candidates"),
    ]
    venue = {1: "kalshi", 2: "kalshi", 3: "forecastex", 4: "forecastex", 5: "forecastex"}
    liq = {1: 300.0, 2: 300.0, 3: 100.0, 4: 100.0, 5: 100.0}
    fx_pr = 0.26 / 0.38
    expected = (0.625 * 600 + fx_pr * 300) / 900
    r = reduce_races(obs, venue, liq)
    assert r["Texas"].p_r == pytest.approx(expected)
    assert r["Texas"].weight == pytest.approx(900.0)
    assert r["Texas"].venue_counts == {"kalshi": 2, "forecastex": 3}


def test_venue_prefers_party_direct_over_candidate() -> None:
    # One venue carrying both: the clean party-direct leg wins; candidate ignored.
    obs = [
        _obs(1, "Iowa", "R", 0.70, "patterns"),
        _obs(2, "Iowa", "R", 0.05, "candidates"),   # would drag a mean down
    ]
    r = reduce_races(obs, {1: "kalshi", 2: "kalshi"}, {1: 100.0, 2: 100.0})
    assert r["Iowa"].p_r == pytest.approx(0.70)
    assert r["Iowa"].market_ids == [1]


def test_all_play_money_simple_mean_across_venues() -> None:
    # No liquidity anywhere (play-money) → simple mean of per-venue P(R).
    obs = [
        _obs(1, "Utah", "R", 0.60, "patterns"),   # manifold venue A
        _obs(2, "Utah", "R", 0.80, "patterns"),   # metaculus venue B
    ]
    r = reduce_races(obs, {1: "manifold", 2: "metaculus"}, {})
    assert r["Utah"].p_r == pytest.approx(0.70)
    assert r["Utah"].weight == 0.0


def test_zero_mass_candidate_venue_skipped() -> None:
    # A venue whose only legs are zero-priced candidates yields no reading.
    obs = [_obs(1, "Nevada", "R", 0.0, "candidates"), _obs(2, "Nevada", "D", 0.0, "candidates")]
    r = reduce_races(obs, {1: "polymarket", 2: "polymarket"}, {})
    assert "Nevada" not in r


def test_single_party_candidates_not_normalised_to_one() -> None:
    # Only R candidates listed (opponent absent on this venue): normalising would
    # force P(R)=1. The summed R win-prob IS the estimate — a lone Sullivan @0.375
    # stays 0.375, and two R (0.89+0.03) stay 0.92 (not 1.0).
    r1 = reduce_races([_obs(1, "Alaska", "R", 0.375, "candidates")], {1: "kalshi"}, {})
    assert r1["Alaska"].p_r == pytest.approx(0.375)
    r2 = reduce_races(
        [_obs(1, "South Dakota-1", "R", 0.89, "candidates"),
         _obs(2, "South Dakota-1", "R", 0.03, "candidates")],
        {1: "forecastex", 2: "forecastex"}, {},
    )
    assert r2["South Dakota-1"].p_r == pytest.approx(0.92)


def test_single_party_d_candidate_is_complement() -> None:
    # Only a D candidate @0.30 → P(R) = 1 - 0.30 = 0.70.
    r = reduce_races([_obs(1, "Maine", "D", 0.30, "candidates")], {1: "polymarket"}, {})
    assert r["Maine"].p_r == pytest.approx(0.70)


def test_llm_fallback_treated_as_party_direct() -> None:
    obs = [_obs(1, "Maine", "D", 0.30, "llm_fallback")]  # prob_r = 0.70
    r = reduce_races(obs, {1: "forecastex"}, {1: 50.0})
    assert r["Maine"].p_r == pytest.approx(0.70)


# --------------------------------------------------------------------------
# 2026-10-12 — tier precedence inside a venue + same-party races (CORR-1.11)
# --------------------------------------------------------------------------

from pmi_core.engine.seat_mapping import (  # noqa: E402
    candidate_identity,
    candidate_label_from_title,
)


def test_venue_patterns_beat_llm_fallback_noise() -> None:
    # Texas on kalshi: the two party legs (R 0.37 / D 0.64) plus llm_fallback
    # noise ("Crockett participates…" D 0.39, a combo R 0.90). The old mean of
    # all four read 0.625 — the Texas "60 · PROJECTED HOLD" card.
    obs = [
        _obs(1, "Texas", "R", 0.37, "patterns"),
        _obs(2, "Texas", "D", 0.64, "patterns"),
        _obs(3, "Texas", "D", 0.39, "llm_fallback"),
        _obs(4, "Texas", "R", 0.90, "llm_fallback"),
    ]
    r = reduce_races(obs, {i: "kalshi" for i in (1, 2, 3, 4)}, {1: 100.0, 2: 100.0, 3: 1.0, 4: 1.0})
    assert r["Texas"].p_r == pytest.approx((0.37 + 0.36) / 2)
    assert set(r["Texas"].market_ids) == {1, 2}


def test_venue_candidates_beat_llm_fallback() -> None:
    obs = [
        _obs(1, "Texas-2", "R", 0.26, "candidates"),
        _obs(2, "Texas-2", "D", 0.12, "candidates"),
        _obs(3, "Texas-2", "R", 0.90, "llm_fallback"),
    ]
    r = reduce_races(obs, {1: "forecastex", 2: "forecastex", 3: "forecastex"}, {})
    assert r["Texas-2"].p_r == pytest.approx(0.26 / 0.38)


def test_llm_fallback_used_when_venue_has_nothing_else() -> None:
    obs = [_obs(1, "Maine", "D", 0.30, "llm_fallback")]
    r = reduce_races(obs, {1: "gemini"}, {})
    assert r["Maine"].p_r == pytest.approx(0.70)


def _cand(mid, key, party, price, label, code=None):
    return EntityObservation(
        market_id=mid, key=key, code=code or key, party=party, price=price,
        tier="candidates", venue_rank=0, label=label,
    )


def test_same_party_race_pools_candidates_across_venues() -> None:
    # CA-11 (Wiener vs Chan, both D): polymarket lists Chan 0.78, kalshi Wiener
    # 0.23, forecastex Chan 0.73. Per-venue complement read 0.22 / 0.77 / 0.27 →
    # a 25-R race with no Republican on the ballot. Pooled per candidate:
    # mean(0.78, 0.73) + 0.23 = 0.985 ≥ 0.9 → same-party → P(R) = 0.015.
    obs = [
        _cand(1, "California-11", "D", 0.78, "Connie Chan", "CA-11"),
        _cand(2, "California-11", "D", 0.23, "Scott Wiener", "CA-11"),
        _cand(3, "California-11", "D", 0.73, "Connie Chan", "CA-11"),
    ]
    r = reduce_races(obs, {1: "polymarket", 2: "kalshi", 3: "forecastex"}, {1: 300.0, 2: 100.0, 3: 50.0})
    assert r["California-11"].p_r == pytest.approx(0.015)


def test_same_party_rule_symmetric_for_republicans() -> None:
    obs = [
        _cand(1, "Texas-2", "R", 0.60, "Dan Crenshaw"),
        _cand(2, "Texas-2", "R", 0.35, "Steve Toth"),
    ]
    r = reduce_races(obs, {1: "gemini", 2: "ibkr"}, {})
    assert r["Texas-2"].p_r == pytest.approx(0.95)


def test_lone_candidate_keeps_complement_when_field_not_spanned() -> None:
    # One D candidate at 0.45 and nothing else listed: the Republican just isn't
    # on this venue — keep 1 − 0.45, don't declare a same-party race.
    obs = [_cand(1, "Ohio", "D", 0.45, "Amy Acton")]
    r = reduce_races(obs, {1: "gemini"}, {})
    assert r["Ohio"].p_r == pytest.approx(0.55)


def test_same_party_rule_off_when_directed_leg_exists() -> None:
    obs = [
        _cand(1, "California-11", "D", 0.78, "Connie Chan", "CA-11"),
        _obs(2, "California-11", "R", 0.001, "patterns"),
    ]
    r = reduce_races(obs, {1: "polymarket", 2: "polymarket"}, {})
    # Inside polymarket the party leg outranks the candidate leg.
    assert r["California-11"].p_r == pytest.approx(0.001)


def test_candidate_label_from_title_shapes() -> None:
    assert candidate_label_from_title("California-11 House Winner - Scott Wiener") == "Scott Wiener"
    assert candidate_label_from_title("CA-11 General Election — Connie Chan") == "Connie Chan"
    assert candidate_label_from_title("Who will win the CA-11 House election? - Connie Chan") == "Connie Chan"
    assert candidate_label_from_title("Will Connie Chan win the CA-11 House seat?") == "Connie Chan"
    assert candidate_label_from_title("Will Todd Achilless win the Senate race in Idaho?") == "Todd Achilless"
    # party tails are not names
    assert candidate_label_from_title("Will Democratic win the House race for AL-01? - Democratic party") is None
    assert candidate_label_from_title("Will the Republicans win the Texas Senate race in 2026?") is None
    assert candidate_label_from_title(None) is None


def test_candidate_identity_is_surname_or_market() -> None:
    assert candidate_identity(_cand(1, "x", "D", 0.5, "Connie Chan")) == "chan"
    assert candidate_identity(_cand(2, "x", "D", 0.5, "Connie Chan (D)")) == "chan"
    assert candidate_identity(_cand(3, "x", "D", 0.5, None)) == "#3"
