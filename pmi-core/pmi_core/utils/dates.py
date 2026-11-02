"""UTC date utilities.

Ported from `micah-db/micah_db/utils/dates.py` (2026-05). Kept as a thin
standalone helper so the bucket collapser and date analyzer don't have to
reach across the workspace into the legacy Micah package.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def utc_today() -> date:
    """Return today's date in UTC, regardless of server timezone."""
    return datetime.now(UTC).date()
