"""Unit tests for the pure helpers in the Insights engine (the SQL engines are
validated against the live DB; these lock the date/delta arithmetic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.engine.insights_compute import _delta_before, _week_key


def _pts(*vals: float) -> list[tuple[datetime, float]]:
    """Daily points ending today, oldest first."""
    now = datetime.now(UTC)
    n = len(vals)
    return [(now - timedelta(days=(n - 1 - i)), v) for i, v in enumerate(vals)]


def test_delta_before_uses_last_point_at_or_before_the_mark():
    # 8 daily points; 7 days ago is the first → delta = last - first.
    pts = _pts(10, 11, 12, 13, 14, 15, 16, 17)
    assert _delta_before(pts, 7) == 7.0


def test_delta_before_none_when_too_short():
    assert _delta_before([], 7) is None
    assert _delta_before(_pts(50.0), 7) is None


def test_delta_before_none_when_no_point_before_mark():
    # Only 3 days of history → nothing at/ before the 7-day mark.
    assert _delta_before(_pts(50, 51, 52), 7) is None


def test_delta_before_rounds():
    pts = _pts(50.0, 50.126)
    assert _delta_before(pts, 1) == 0.13


def test_week_key_is_iso_year_week():
    assert _week_key(datetime(2026, 9, 13, tzinfo=UTC)) == "2026-W37"
    assert _week_key(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-W01"
