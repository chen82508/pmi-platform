"""IR validation tests for SeatProjectionSpec (CORR-1.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmi_core.dsl.ir import AggregationSpec, SeatProjectionSpec


def test_defaults_are_senate_shape():
    sp = SeatProjectionSpec()
    assert sp.total_seats == 100
    assert sp.majority_threshold == 51
    assert sp.holdover_r == 0
    assert sp.holdover_d == 0


def test_aggregation_seat_projection_optional():
    # Indexes that aren't seat projections omit the block entirely.
    assert AggregationSpec().seat_projection is None


def test_valid_senate_geometry():
    sp = SeatProjectionSpec(
        total_seats=100, majority_threshold=51, holdover_r=30, holdover_d=37
    )
    assert sp.holdover_r + sp.holdover_d == 67


def test_holdover_cannot_exceed_total():
    with pytest.raises(ValidationError, match="exceeds total_seats"):
        SeatProjectionSpec(total_seats=100, holdover_r=60, holdover_d=60)


def test_threshold_cannot_exceed_total():
    with pytest.raises(ValidationError, match="majority_threshold"):
        SeatProjectionSpec(total_seats=100, majority_threshold=120)


def test_house_zero_holdover_geometry():
    sp = SeatProjectionSpec(
        total_seats=435, majority_threshold=218, holdover_r=0, holdover_d=0
    )
    assert sp.total_seats == 435
    assert sp.majority_threshold == 218


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        SeatProjectionSpec(total_seats=100, bogus=1)  # type: ignore[call-arg]
