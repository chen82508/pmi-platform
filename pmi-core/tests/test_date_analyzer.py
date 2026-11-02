"""Unit tests for the ported `pmi_core.utils.date_analyzer`.

These tests cover only the bucket-related surface used by
``pmi_core.engine.bucket_collapser`` — full Micah parity isn't required.
The original test suite lives in ``micah-db`` and exercises additional
edge cases (e.g. 2-digit years, DMY/MDY priority); those can be ported
later if needed.
"""

from __future__ import annotations

from datetime import date

from pmi_core.utils.date_analyzer import (
    is_active_bucket,
    parse_bucket_date,
    strip_date_suffix,
    title_refers_to_past,
)

REF_DATE = date(2026, 6, 15)


class TestStripDateSuffix:
    def test_on_date(self) -> None:
        assert (
            strip_date_suffix("Will the US strike Iran on February 21, 2026 (ET)?")
            == "Will the US strike Iran"
        )

    def test_on_or_after(self) -> None:
        assert (
            strip_date_suffix("Will the US strike Iran on or after March 1, 2026?")
            == "Will the US strike Iran"
        )

    def test_during_week(self) -> None:
        assert (
            strip_date_suffix(
                "Will the US strike Iran during the week of February 15-21, 2026?"
            )
            == "Will the US strike Iran"
        )

    def test_in_year(self) -> None:
        assert (
            strip_date_suffix("Khamenei out as Supreme Leader of Iran in 2026?")
            == "Khamenei out as Supreme Leader of Iran"
        )

    def test_by_end_of(self) -> None:
        assert (
            strip_date_suffix(
                "Will Ethiopia bring a fifth generation fighter into service by the end of 2030?"
            )
            == "Will Ethiopia bring a fifth generation fighter into service"
        )

    def test_no_date_returns_original(self) -> None:
        assert strip_date_suffix("Will Russia invade?") == "Will Russia invade?"


class TestParseBucketDate:
    def test_mdy(self) -> None:
        assert (
            parse_bucket_date("Will X happen on February 21, 2026 (ET)?")
            == date(2026, 2, 21)
        )

    def test_dmy(self) -> None:
        assert parse_bucket_date("Will X happen on 14 January 2026?") == date(2026, 1, 14)

    def test_week_range_picks_end(self) -> None:
        # "during the week of Feb 15-21, 2026" → end day = 21
        assert (
            parse_bucket_date(
                "Will X happen during the week of February 15-21, 2026?"
            )
            == date(2026, 2, 21)
        )

    def test_no_date_returns_none(self) -> None:
        assert parse_bucket_date("Will X happen?") is None


class TestIsActiveBucket:
    def test_future_date_is_active(self) -> None:
        assert is_active_bucket("Will X happen on August 1, 2026?", REF_DATE) is True

    def test_past_date_is_inactive(self) -> None:
        assert is_active_bucket("Will X happen on January 1, 2026?", REF_DATE) is False

    def test_today_is_active(self) -> None:
        assert is_active_bucket("Will X happen on June 15, 2026?", REF_DATE) is True

    def test_unparseable_treated_as_active(self) -> None:
        assert is_active_bucket("Will X happen?", REF_DATE) is True


class TestTitleRefersToPast:
    def test_past_year(self) -> None:
        assert title_refers_to_past("Did X happen in 2020?") is True

    def test_future_year(self) -> None:
        assert title_refers_to_past("Will X happen in 2099?") is False

    def test_no_year(self) -> None:
        assert title_refers_to_past("Will X ever happen?") is False
