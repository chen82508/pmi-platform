"""`_line` must never hand the upsert a value Numeric(6,2) cannot hold.

tag-games upserts 400 tag rows per statement; one out-of-range line_value
(2026-10-20: numeric field overflow) failed the whole chunk, three runs in a
row, and the Slack failure hook was itself broken — so the guard lives at the
parser, where a bad line costs one column instead of 400 tags.
"""

from __future__ import annotations

import pytest

from pmi_core.engine.sports_tag import LINE_ABS_MAX, _line


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("8.5", 8.5), (-1.5, -1.5), (9999.99, 9999.99), (0, 0.0), ("215.5", 215.5)],
)
def test_line_in_range_parses(raw: object, expected: float) -> None:
    assert _line({"line_value": raw}) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "abc", LINE_ABS_MAX, -LINE_ABS_MAX, 20261020, "1e9", float("inf"), float("nan")],
)
def test_line_out_of_range_or_malformed_is_dropped(raw: object) -> None:
    assert _line({"line_value": raw}) is None


def test_line_missing_key_is_none() -> None:
    assert _line({}) is None
