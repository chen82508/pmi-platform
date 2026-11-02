"""Date analysis utilities for market titles.

Ported from `micah-db/micah_db/utils/date_analyzer.py` (2026-05-29 snapshot).

Parses dates from prediction-market contract titles and determines whether
a market refers to a past event. Supports MDY ("February 21, 2026") and
DMY ("14 Jan 2026") formats, abbreviated month names, ordinal suffixes,
2-digit years, and year-less dates (e.g., "March 7" assumes current year).

Used by the platform's bucket collapser to:
  1. Strip a daily/weekly bucket suffix from a market title to derive the
     `base_question` (the dedupe key).
  2. Parse the bucket's end date to (a) drop expired buckets and (b) split
     groups whose spread exceeds `max_spread_days`.

Self-contained: no DB, no third-party deps beyond stdlib `re` / `datetime`.
"""

from __future__ import annotations

import re
from datetime import date

from pmi_core.utils.dates import utc_today

# ---------------------------------------------------------------------------
# Month / date regex building blocks
# ---------------------------------------------------------------------------

_MONTH_NAMES = (
    r"January|February|March|April|May|June|July|August|September|"
    r"October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
)

# Optional ordinal suffix on day numbers: 1st, 2nd, 3rd, 4th, 21st, …
_ORD = r"(?:st|nd|rd|th)"

# MDY: "February 21, 2026" | "Feb 15-21, 2026" | "March 29-April 4, 2026"
# Also year-less: "March 7" | "Feb 15-21" (year assumed to be current year)
_DATE_MDY = (
    rf"(?:{_MONTH_NAMES})\s+\d{{1,2}}{_ORD}?"
    rf"(?:-(?:(?:{_MONTH_NAMES})\s+)?\d{{1,2}}{_ORD}?)?"
    rf"(?:,?\s+\d{{2,4}})?"
)

# DMY: "14 January 2026" | "14th Jan 26" | "1st Feb 2026"
_DATE_DMY = (
    rf"\d{{1,2}}{_ORD}?\s+(?:{_MONTH_NAMES})"
    rf"(?:,?\s+\d{{2,4}})?"
)

# Month + Year (no day): "July 2026", "March 2027"
_DATE_MONTH_YEAR = rf"(?:{_MONTH_NAMES})\s+\d{{4}}"

# Bare year: "2026", "2027"
_DATE_BARE_YEAR = r"\d{4}"

_DATE_LITERAL = rf"(?:{_DATE_MDY}|{_DATE_DMY}|{_DATE_MONTH_YEAR}|{_DATE_BARE_YEAR})"

# ---------------------------------------------------------------------------
# Date-bucket suffix pattern (used by strip_date_suffix)
# ---------------------------------------------------------------------------

# Matches date-bucket suffixes at the end of a contract title.
# Requires a preposition keyword before the date — this prevents false positives.
DATE_BUCKET_PATTERN = re.compile(
    r"\s+"
    r"(?:on\s+or\s+(?:before|after)|during\s+the\s+week\s+of|by\s+end\s+of|by\s+the\s+end\s+of|on|before|after|between|by)"
    r"\s+"
    + _DATE_LITERAL
    + r"(?:\s+and\s+"
    + _DATE_LITERAL
    + r")?"
    + r"(?:\s*\([A-Z]{2,4}\))?"
    + r"\s*\??\s*$",
    re.IGNORECASE,
)

# Matches "in YYYY" or "in Month YYYY" at the end of a title. Kept separate
# from DATE_BUCKET_PATTERN because "in" is too common a preposition — we only
# want to strip it when followed by a bare year.
IN_YEAR_SUFFIX_PATTERN = re.compile(
    r"\s+in\s+"
    + rf"(?:(?:{_MONTH_NAMES})\s+)?"
    + r"\d{4}"
    + r"(?:\s*\([A-Z]{2,4}\))?"
    + r"\s*\??\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Date extraction regexes
# ---------------------------------------------------------------------------

# MDY extraction: captures (month, start_day, end_month?, end_day?, year?)
# Anchored to end of string so it picks the last date in "between X and Y".
_BUCKET_DATE_MDY_RE = re.compile(
    rf"({_MONTH_NAMES})\s+(\d{{1,2}}){_ORD}?"
    rf"(?:-(?:({_MONTH_NAMES})\s+)?(\d{{1,2}}){_ORD}?)?"
    rf"(?:,?\s+(\d{{2,4}}))?"
    rf"(?:\s*\([A-Z]{{2,4}}\))?"
    rf"\s*\??\s*$",
    re.IGNORECASE,
)

# DMY extraction: captures (day, month, year?)
_BUCKET_DATE_DMY_RE = re.compile(
    rf"(\d{{1,2}}){_ORD}?\s+({_MONTH_NAMES})"
    rf"(?:,?\s+(\d{{2,4}}))?"
    rf"(?:\s*\([A-Z]{{2,4}}\))?"
    rf"\s*\??\s*$",
    re.IGNORECASE,
)

# Year-only regex for title_refers_to_past
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# ---------------------------------------------------------------------------
# Month lookup + helpers
# ---------------------------------------------------------------------------

_MONTH_LOOKUP: dict[str, int] = {}
for _i, _name in enumerate(
    [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ],
    start=1,
):
    _MONTH_LOOKUP[_name] = _i
    _MONTH_LOOKUP[_name[:3]] = _i


def _resolve_year(year_str: str) -> int:
    """Resolve 2-digit or 4-digit year string to a full year."""
    y = int(year_str)
    return y + 2000 if y < 100 else y


def _extract_mdy(m: re.Match[str], default_year: int | None = None) -> date | None:
    """Extract a date from an MDY regex match."""
    month_name = m.group(3) or m.group(1)
    day = int(m.group(4) or m.group(2))
    month = _MONTH_LOOKUP.get(month_name.lower())
    if month is None:
        return None
    year_str = m.group(5)
    year = _resolve_year(year_str) if year_str else (default_year or utc_today().year)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _extract_dmy(m: re.Match[str], default_year: int | None = None) -> date | None:
    """Extract a date from a DMY regex match."""
    day = int(m.group(1))
    month = _MONTH_LOOKUP.get(m.group(2).lower())
    if month is None:
        return None
    year_str = m.group(3)
    year = _resolve_year(year_str) if year_str else (default_year or utc_today().year)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_bucket_date(title: str) -> date | None:
    """Extract the end date from a daily/weekly bucket title suffix.

    Supports MDY ("February 21, 2026", "Feb 15-21, 2026") and DMY
    ("14 January 2026", "14th Jan 26") formats, plus year-less variants
    ("March 7", "7th March") which assume the current year.

    Only parses titles that have a recognized bucket suffix (preposition +
    date at end of string). Returns None for non-bucket titles.
    """
    if strip_date_suffix(title) == title:
        return None

    m_mdy = _BUCKET_DATE_MDY_RE.search(title)
    m_dmy = _BUCKET_DATE_DMY_RE.search(title)

    # Prefer matches with an explicit year over year-less ones — prevents
    # "Jan 26" (MDY year-less) from beating "14 Jan 26" (DMY with year).
    if m_mdy and m_mdy.group(5) is not None:
        return _extract_mdy(m_mdy)
    if m_dmy and m_dmy.group(3) is not None:
        return _extract_dmy(m_dmy)
    if m_mdy:
        return _extract_mdy(m_mdy)
    if m_dmy:
        return _extract_dmy(m_dmy)
    return None


def is_active_bucket(title: str, today: date) -> bool:
    """Check whether a bucket's title date is today or in the future."""
    bucket_date = parse_bucket_date(title)
    if bucket_date is None:
        return True  # un-parseable → treat as active (safe fallback)
    return bucket_date >= today


def strip_date_suffix(title: str) -> str:
    """Strip daily-bucket date suffix from a contract title.

    Returns the base question (title with the date portion removed).
    If no date suffix is found, returns the original title unchanged.

    Examples:
        "Will the US next strike Iran on February 21, 2026 (ET)?"
        -> "Will the US next strike Iran"

        "Khamenei out as Supreme Leader of Iran in 2026?"
        -> "Khamenei out as Supreme Leader of Iran"
    """
    stripped = DATE_BUCKET_PATTERN.sub("", title).strip()
    if stripped and stripped != title:
        return stripped
    stripped = IN_YEAR_SUFFIX_PATTERN.sub("", title).strip()
    return stripped if stripped else title


def title_refers_to_past(title: str) -> bool:
    """Return True if every year mentioned in the title is before the current year."""
    years = _YEAR_RE.findall(title)
    if not years:
        return False
    return max(int(y) for y in years) < utc_today().year
