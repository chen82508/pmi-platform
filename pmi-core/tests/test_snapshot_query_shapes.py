"""Shape tests for the snapshot queries rewritten on 2026-10-20.

esports-snapshot averaged 672 s and gdp-board-snapshot 368 s. pg_stat_statements
on the data box put the time in three statements: the esports winner-candidate
OR across three venues (~360 s, planner walked every open market of all three
venues), the per-match holdings OR (~3 s x ~100 matches) and the GDP seed
DISTINCT ON over every historical snapshot of a period's members (70-140 s x
16 periods). These tests pin the index-friendly shapes that replaced them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from pmi_core.engine import esports_compute as ec
from pmi_core.engine.gdp_compute import seed_snapshot_stmt
from pmi_core.models import CoreMarket


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_gdp_seed_is_one_lateral_probe_per_market() -> None:
    sql = _sql(seed_snapshot_stmt([1, 2, 3], datetime(2026, 10, 6, tzinfo=UTC)))
    assert "LATERAL" in sql
    assert "LIMIT 1" in sql
    assert "unnest" in sql
    assert "snapshot_at <" in sql
    assert "DISTINCT ON" not in sql


def test_winner_branches_are_one_venue_each() -> None:
    branches = ec.winner_market_branches("cs2|lol")
    assert len(branches) == 3
    sqls = [_sql(select(CoreMarket.id).where(b)) for b in branches]
    for sql in sqls:
        assert sql.count("core_markets.venue =") == 1, sql
    kalshi, robinhood, polymarket = sqls
    assert "'kalshi'" in kalshi and "~*" in kalshi and "LIKE" not in kalshi
    assert "'robinhood'" in robinhood and "match" in robinhood
    assert "'polymarket'" in polymarket and "Match Winner" in polymarket


def test_holdings_split_one_statement_per_or_arm() -> None:
    base = select(CoreMarket.id)
    name_conds = [CoreMarket.title.ilike("%Donstu%"), CoreMarket.title.ilike("%TitanFlow%")]
    with_ids = ec.holdings_stmts(base, name_conds, [1454177350, 1454177351])
    assert len(with_ids) == 2
    names_sql, ids_sql = (_sql(s) for s in with_ids)
    assert " OR " not in names_sql
    assert "core_markets.id IN" in ids_sql
    assert len(ec.holdings_stmts(base, name_conds, [])) == 1


def test_clean_market_ids_dedupes_and_drops_junk() -> None:
    assert ec.clean_market_ids([3, "1", None, "x", 1, 2.0]) == [1, 2, 3]
    assert ec.clean_market_ids([]) == []
