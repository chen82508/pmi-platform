"""Unit tests for the date-aware bucket collapser.

Ported from Micah's `tests/jobs/workflows/evaluate_contracts/test_bucket_collapser.py`
(2026-05-29 — PR #15) and adapted for the platform's `MarketEvaluations`
shape. Pure-Python, no DB / MLflow / network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from pmi_core.dsl.ir import (
    AggregationSpec,
    CollapseSpec,
    FactorSpec,
    IndexDef,
    KeywordSelector,
)
from pmi_core.engine.aggregator import MarketEvaluations
from pmi_core.engine.bucket_collapser import (
    BucketGroup,
    collapse_for_scoring,
    detect_bucket_groups,
)
from pmi_core.models import CoreMarket

REF_DATE = date(2026, 6, 15)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _ir(*, max_spread_days: int = 30, enabled: bool = True) -> IndexDef:
    """Minimal IndexDef whose collapse settings exercise the new path."""
    return IndexDef(
        id="test-index",
        version=1,
        title="Test Index",
        owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["unused"])],
        factors=[
            FactorSpec(
                id="direct_link",
                type="binary",
                prompt_ref="prompts/factors/direct_link-v1",
                weight=40.0,
            ),
        ],
        aggregation=AggregationSpec(
            collapse=CollapseSpec(
                enabled=enabled,
                max_spread_days=max_spread_days,
                representative="max_probability",
            ),
            min_components=1,
        ),
    )


def _row(
    market_id: int,
    title: str,
    *,
    last_price: float = 0.10,
    venue: str = "polymarket",
) -> MarketEvaluations:
    """Build a minimal `MarketEvaluations` with no factor evals attached."""
    market = CoreMarket(
        id=market_id,
        venue=venue,
        external_id=f"ext-{market_id}",
        slug=None,
        title=title,
        description=None,
        category=None,
        tags=None,
        opens_at=None,
        closes_at=None,
        resolved_at=None,
        raw=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return MarketEvaluations(market=market, by_factor={}, last_price=last_price)


# --------------------------------------------------------------------------
# detect_bucket_groups
# --------------------------------------------------------------------------


class TestDetectBucketGroups:
    def test_no_date_suffix_no_groups(self) -> None:
        rows = [
            _row(1, "Will Iran attack Israel?"),
            _row(2, "Will Russia invade Ukraine?"),
        ]
        assert detect_bucket_groups(rows, _ir(), ref_date=REF_DATE) == []

    def test_two_active_buckets_form_group(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?"),
            _row(2, "Will the US strike Iran on June 21, 2026?"),
        ]
        groups = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)
        assert len(groups) == 1
        assert set(groups[0].member_indices) == {0, 1}

    def test_expired_buckets_excluded(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on May 1, 2026?"),
            _row(2, "Will the US strike Iran on May 2, 2026?"),
        ]
        # Both before REF_DATE → expired
        assert detect_bucket_groups(rows, _ir(), ref_date=REF_DATE) == []

    def test_singleton_active_bucket_not_a_group(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?"),
            _row(2, "Will Russia attack Ukraine?"),
        ]
        assert detect_bucket_groups(rows, _ir(), ref_date=REF_DATE) == []

    def test_representative_is_highest_probability(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.02),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.08),
        ]
        groups = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)
        assert groups[0].representative_idx == 1

    def test_calendar_buckets_use_noisy_or(self) -> None:
        # Mirrors Micah's `test_calendar_buckets_use_noisy_or`:
        # p_collapsed = 1 - (1-0.7)*(1-0.7) = 0.91
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.7),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.7),
        ]
        groups = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)
        assert groups[0].mode == "noisy_or"
        assert abs(groups[0].collapsed_probability - 0.91) < 1e-9

    def test_multi_year_deadline_uses_average(self) -> None:
        # 3 deadlines > 30 days apart → mean, not noisy-OR
        fighter = (
            "Will Ethiopia bring a fifth generation fighter into service by the end of"
        )
        rows = [
            _row(1, f"{fighter} 2040?", last_price=0.576),
            _row(2, f"{fighter} 2035?", last_price=0.584),
            _row(3, f"{fighter} 2030?", last_price=0.324),
        ]
        groups = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)
        assert len(groups) == 1
        assert groups[0].mode == "mean"
        expected = (0.576 + 0.584 + 0.324) / 3
        assert abs(groups[0].collapsed_probability - expected) < 1e-9

    def test_buckets_too_far_apart_split_into_sub_groups(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?"),
            _row(2, "Will the US strike Iran on June 21, 2026?"),
            # > 30 days away — same base question but new sub-group
            _row(3, "Will the US strike Iran on August 1, 2026?"),
            _row(4, "Will the US strike Iran on August 2, 2026?"),
        ]
        groups = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)
        assert len(groups) == 2

    def test_collapse_disabled_returns_empty(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?"),
            _row(2, "Will the US strike Iran on June 21, 2026?"),
        ]
        assert detect_bucket_groups(rows, _ir(enabled=False), ref_date=REF_DATE) == []


# --------------------------------------------------------------------------
# collapse_for_scoring
# --------------------------------------------------------------------------


class TestCollapseForScoring:
    def test_no_buckets_returns_same_list(self) -> None:
        rows = [_row(1, "No date"), _row(2, "Also no date")]
        result = collapse_for_scoring(rows, _ir(), ref_date=REF_DATE)
        assert [r.market.id for r in result] == [1, 2]

    def test_bucket_group_collapsed_to_single_entry(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.08),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.02),
        ]
        result = collapse_for_scoring(rows, _ir(), ref_date=REF_DATE)
        assert len(result) == 1

    def test_synthetic_has_collapsed_probability(self) -> None:
        # noisy-OR(0.08, 0.02) = 1 - 0.92*0.98 = 0.0984
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.08),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.02),
        ]
        result = collapse_for_scoring(rows, _ir(), ref_date=REF_DATE)
        assert result[0].last_price is not None
        assert abs(result[0].last_price - 0.0984) < 1e-4

    def test_non_bucket_rows_preserved(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.08),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.02),
            _row(99, "Unrelated market"),
        ]
        result = collapse_for_scoring(rows, _ir(), ref_date=REF_DATE)
        # rep (id=1) + unrelated (id=99) = 2 rows
        ids = sorted(r.market.id for r in result)
        assert ids == [1, 99]

    def test_synthetic_keeps_representative_market_identity(self) -> None:
        """Lineage: synthetic row points at the representative market (rep wins)."""
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.08),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.02),
        ]
        result = collapse_for_scoring(rows, _ir(), ref_date=REF_DATE)
        # rep = highest last_price = id 1
        assert result[0].market.id == 1
        assert result[0].market.external_id == "ext-1"

    def test_collapse_disabled_passthrough(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?"),
            _row(2, "Will the US strike Iran on June 21, 2026?"),
        ]
        result = collapse_for_scoring(rows, _ir(enabled=False), ref_date=REF_DATE)
        assert len(result) == 2


# --------------------------------------------------------------------------
# BucketGroup dataclass invariants
# --------------------------------------------------------------------------


class TestBucketGroupDataclass:
    def test_mode_label_propagates_into_group(self) -> None:
        rows = [
            _row(1, "Will the US strike Iran on June 20, 2026?", last_price=0.5),
            _row(2, "Will the US strike Iran on June 21, 2026?", last_price=0.5),
        ]
        g = detect_bucket_groups(rows, _ir(), ref_date=REF_DATE)[0]
        assert isinstance(g, BucketGroup)
        assert g.mode == "noisy_or"
        assert g.base_question == "Will the US strike Iran"
