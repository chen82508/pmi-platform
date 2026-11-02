"""Pure helpers of pmi_core.market_priority (the SQL itself is exercised against a
real Postgres in pmi-workers/tests/test_refresh_market_priority.py)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from pmi_core import market_priority as mp

NOW = datetime(2026, 10, 11, tzinfo=UTC)


def test_watch_rows_dedupe_and_sort() -> None:
    rows = mp.watch_rows("board:x", [3, 1, 3], NOW)
    assert [r["market_id"] for r in rows] == [1, 3]
    assert all(r["watcher"] == "board:x" and r["expires_at"] == NOW for r in rows)


def test_watch_rows_empty() -> None:
    assert mp.watch_rows("board:x", [], NOW) == []


def test_activity_watcher_name_carries_the_tier_prefix() -> None:
    assert mp.activity_watcher("kalshi") == "activity:kalshi"
    assert mp.activity_watcher("kalshi").startswith(mp.ACTIVITY_WATCHER_PREFIX)


def test_count_tiers() -> None:
    assert mp.count_tiers([0, 0, 2, 1, 0]) == {0: 3, 2: 1, 1: 1}
    assert mp.count_tiers([]) == {}


def test_refresh_result_total() -> None:
    assert mp.RefreshResult(by_tier={0: 2, 1: 3}).total == 5
    assert mp.RefreshResult().total == 0


def test_upsert_never_filters_core_markets_by_hot_columns() -> None:
    """core_markets may only be reached by PK here (0017: hot columns unindexed)."""
    sql = str(mp._UPSERT_PRIORITY.compile(dialect=postgresql.dialect()))
    for hot in ("last_price_at", "updated_at", "created_at"):
        assert hot not in sql
    assert "JOIN core_markets m ON m.id = s.market_id" in sql
    assert "WHERE m.id > CAST(" in sql
