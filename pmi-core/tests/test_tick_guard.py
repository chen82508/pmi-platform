"""Partial-tick guard: when a collapsed member set may and may not publish."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.engine.tick_guard import partial_tick_verdict

NOW = datetime(2026, 10, 6, 8, 34, tzinfo=UTC)
KW = dict(min_ratio=0.5, min_baseline=20, max_hold_hours=6.0)


def test_collapsed_tick_is_held_when_a_recent_score_exists() -> None:
    """The 2026-10-06 incident: 28 members against a ~180 baseline, last score 34 min old."""
    v = partial_tick_verdict(
        28, [179, 183, 185, 176, 181],
        latest_published_at=NOW - timedelta(minutes=34), now=NOW, **KW,
    )
    assert v.partial and not v.publish
    assert v.reason == "held"
    assert v.baseline_median == 181
    assert v.baseline_n == 5


def test_normal_tick_publishes() -> None:
    v = partial_tick_verdict(
        176, [179, 183, 185, 176, 181],
        latest_published_at=NOW - timedelta(hours=1), now=NOW, **KW,
    )
    assert not v.partial and v.publish and v.reason == "ok"


def test_exactly_at_ratio_publishes() -> None:
    v = partial_tick_verdict(90, [180], latest_published_at=NOW, now=NOW, **KW)
    assert v.publish and v.reason == "ok"


def test_no_baseline_publishes() -> None:
    """A fresh index version has no history to compare against."""
    v = partial_tick_verdict(3, [], latest_published_at=None, now=NOW, **KW)
    assert v.publish and not v.partial and v.reason == "no_baseline"
    assert v.baseline_median is None


def test_tiny_index_is_not_guarded() -> None:
    """Ratios on a handful of members are noise; min_baseline turns the guard off."""
    v = partial_tick_verdict(2, [8, 9, 10], latest_published_at=NOW, now=NOW, **KW)
    assert v.publish and not v.partial and v.reason == "baseline_too_small"


def test_hold_expires_so_a_shrunken_index_does_not_freeze() -> None:
    """After max_hold_hours without a published score the tick goes out, flagged."""
    v = partial_tick_verdict(
        28, [180] * 10,
        latest_published_at=NOW - timedelta(hours=6, minutes=1), now=NOW, **KW,
    )
    assert v.partial and v.publish and v.reason == "hold_expired"
    assert v.latest_published_age_h is not None and v.latest_published_age_h > 6


def test_hold_still_active_just_before_expiry() -> None:
    v = partial_tick_verdict(
        28, [180] * 10,
        latest_published_at=NOW - timedelta(hours=5, minutes=59), now=NOW, **KW,
    )
    assert v.reason == "held" and not v.publish


def test_median_resists_one_earlier_partial_row() -> None:
    """One already-published collapsed row must not drag the baseline down."""
    v = partial_tick_verdict(
        30, [180, 28, 182, 179, 185], latest_published_at=NOW, now=NOW, **KW
    )
    assert v.reason == "held"
    assert v.baseline_median == 180


def test_to_dict_is_json_friendly() -> None:
    v = partial_tick_verdict(28, [180], latest_published_at=NOW, now=NOW, **KW)
    d = v.to_dict()
    assert d["reason"] == "held" and d["component_count"] == 28
    assert set(d) >= {"partial", "publish", "baseline_median", "baseline_n", "min_ratio"}
