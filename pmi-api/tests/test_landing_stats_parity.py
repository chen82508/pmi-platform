"""Parity tests: /landing/stats' skip-scan `board` step vs the DISTINCT ON original.

PR-A3 replaced `DISTINCT ON (cache_key) ... ORDER BY cache_key, computed_at DESC`
(every row of the 2-day window fetched from the heap) with an index skip scan and
`LATERAL ... LIMIT 1` per key. The rewrite must pick exactly the same snapshot
rows, so these tests run the frozen original and the current query against the
same data and require identical results, on a fixture built to hit the edge
cases: keys with versions inside and outside the window, keys that only exist
outside it, every payload shape the harvest walks, synthetic negative ids and
payloads with no ids at all.

The same comparison was run once against the dev box's live data before merge
(identical key selection and output; see docs/reports, PR-A3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pmi_core.models import CoreMarket, TsPageCache
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pmi_api.routes.landing import (
    _BOARD_LATEST_CTES,
    _STATS_SQL,
    PUBLISHED_INDEX_IDS,
)

# Frozen copy of `_STATS_SQL` as it was before PR-A3 (origin/main c53a201).
# Do not edit. If the stats logic is changed on purpose later, these parity
# tests have served their job: delete them rather than updating this copy.
LEGACY_STATS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (d.index_id)
           d.index_id, s.score, s.as_of, s.component_evaluation_ids AS ids
    FROM core_index_definitions d
    JOIN LATERAL (
        SELECT score, as_of, component_evaluation_ids
        FROM ts_index_scores t
        WHERE t.index_definition_id = d.id
        ORDER BY as_of DESC LIMIT 1
    ) s ON true
    WHERE d.is_current AND d.index_id = ANY(CAST(:index_ids AS text[]))
    ORDER BY d.index_id, d.version DESC
),
board AS (
    SELECT DISTINCT ON (cache_key) payload
    FROM ts_page_cache
    WHERE computed_at > now() - interval '2 days'
    ORDER BY cache_key, computed_at DESC
),
board_mids AS (
    -- `$.**.market_id` catches every board shape (holdings[].market_id,
    -- entities[].contracts[].market_id, …). gdp's top-level `market_ids[]`
    -- array is already unioned in via :gdp_ids below, so we don't also walk
    -- for `$.**.market_ids[*]` — that second recursive pass roughly doubled the
    -- harvest cost for a set we already have.
    SELECT (jp #>> '{}')::bigint AS market_id
    FROM board, LATERAL jsonb_path_query(payload, '$.**.market_id') jp
),
mids AS (
    SELECT DISTINCT ae.market_id
    FROM latest l JOIN audit_evaluations ae ON ae.id = ANY (l.ids)
    UNION
    SELECT unnest(CAST(:gdp_ids AS bigint[]))
    UNION
    SELECT market_id FROM board_mids WHERE market_id > 0
)
SELECT (SELECT count(*) FROM mids) AS contracts,
       (SELECT array_agg(index_id) FROM latest WHERE score IS NOT NULL) AS scored,
       (SELECT max(as_of) FROM latest) AS as_of,
       -- The whole ingested corpus step 1 selects FROM, not the readings'
       -- lineage. `reltuples` (the planner's estimate, refreshed by autovacuum)
       -- rather than count(*): this is served on the hot path of every landing
       -- and /methodology render, core_markets is ~2M rows, and the figure is
       -- displayed rounded to the nearest hundred thousand anyway.
       (SELECT reltuples::bigint FROM pg_class
         WHERE oid = 'core_markets'::regclass) AS universe,
       (SELECT array_agg(v ORDER BY v) FROM (
            SELECT DISTINCT m.venue AS v FROM mids
            JOIN core_markets m ON m.id = mids.market_id
            WHERE m.venue IS NOT NULL
        ) q) AS venues
"""

LEGACY_KEYS = """
SELECT DISTINCT ON (cache_key) cache_key, computed_at
FROM ts_page_cache
WHERE computed_at > now() - interval '2 days'
ORDER BY cache_key, computed_at DESC
"""
CURRENT_KEYS = "WITH RECURSIVE " + _BOARD_LATEST_CTES + " SELECT cache_key, computed_at FROM board"

_FINAL = "\nSELECT (SELECT count(*) FROM mids)"
LEGACY_MIDS = LEGACY_STATS_SQL.split(_FINAL)[0] + " SELECT market_id FROM mids"
CURRENT_MIDS = _STATS_SQL.text.split(_FINAL)[0] + " SELECT market_id FROM mids"

PREFIX = "parity-test:"


@pytest_asyncio.fixture
async def board_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
):
    """Board snapshots covering the edge cases, plus markets with telltale venues.

    A market only reachable through a snapshot that must NOT be picked gets a
    venue of its own (`stale-venue`, `old-venue`), so picking the wrong row shows
    up in `venues` and `contracts`, not just in the key comparison.
    """
    now = datetime.now(UTC)
    venues = {
        "newest": "kalshi",        # multi-version key, newest row (in window)
        "middle": "middle-venue",  # same key, older row still in the window
        "old": "old-venue",        # same key, row outside the window
        "stale": "stale-venue",    # key whose only row is outside the window
        "nested": "gemini",        # deep nesting
        "top": "robinhood",        # top-level market_id
        "edge": "edge-venue",      # key whose only row is just inside the window
        "string": "string-venue",  # market_id serialised as a string
    }
    async with session_factory() as session:
        ids: dict[str, int] = {}
        for name, venue in venues.items():
            market = CoreMarket(
                venue=venue,
                external_id=f"{PREFIX}{name}",
                slug=f"parity-{name}",
                title=f"Parity fixture market {name}",
                description="parity test",
            )
            session.add(market)
            await session.flush()
            ids[name] = market.id

        rows = [
            (f"{PREFIX}multi", now - timedelta(days=3), {"holdings": [{"market_id": ids["old"]}]}),
            (f"{PREFIX}multi", now - timedelta(hours=30), {"holdings": [{"market_id": ids["middle"]}]}),
            (
                f"{PREFIX}multi",
                now - timedelta(hours=1),
                {"entities": [{"contracts": [{"market_id": ids["newest"]}]}]},
            ),
            (f"{PREFIX}stale", now - timedelta(days=5), {"rows": [{"market_id": ids["stale"]}]}),
            (
                f"{PREFIX}nested",
                now - timedelta(hours=2),
                {"a": {"b": [{"c": {"market_id": ids["nested"]}}]}, "market_id": ids["top"]},
            ),
            (f"{PREFIX}synthetic", now - timedelta(hours=1), {"races": [{"market_id": -1}, {"market_id": -2}]}),
            (f"{PREFIX}no-ids", now - timedelta(hours=1), {"score": 42, "series": [1, 2, 3]}),
            (f"{PREFIX}edge", now - timedelta(hours=47), {"holdings": [{"market_id": ids["edge"]}]}),
            (f"{PREFIX}string-id", now - timedelta(hours=1), {"rows": [{"market_id": str(ids["string"])}]}),
        ]
        for key, at, payload in rows:
            session.add(TsPageCache(cache_key=key, payload=payload, computed_at=at))
        await session.commit()

    yield {"ids": ids, "venues": venues}

    async with session_factory() as session:
        await session.execute(delete(TsPageCache).where(TsPageCache.cache_key.like(f"{PREFIX}%")))
        await session.execute(delete(CoreMarket).where(CoreMarket.external_id.like(f"{PREFIX}%")))
        await session.commit()


async def _rows(session: AsyncSession, sql: str, params: dict | None = None) -> list:
    return list((await session.execute(text(sql), params or {})).all())


def _stats_params(gdp_ids: list[int]) -> dict:
    return {"index_ids": PUBLISHED_INDEX_IDS, "gdp_ids": gdp_ids}


@pytest.mark.asyncio
async def test_board_key_selection_matches_legacy(
    session_factory: async_sessionmaker[AsyncSession], board_fixture: dict
) -> None:
    async with session_factory() as session:
        legacy = set(await _rows(session, LEGACY_KEYS))
        current = set(await _rows(session, CURRENT_KEYS))

    assert current == legacy

    picked = {key: at for key, at in current if key.startswith(PREFIX)}
    # The multi-version key resolves to its newest row only.
    assert picked[f"{PREFIX}multi"] == max(
        at for key, at in legacy if key == f"{PREFIX}multi"
    )
    # A key whose snapshots all aged out is dropped; one just inside is kept.
    assert f"{PREFIX}stale" not in picked
    assert f"{PREFIX}edge" in picked
    # 7 fixture keys, minus the aged-out one.
    assert len(picked) == 6


@pytest.mark.asyncio
async def test_stats_sql_matches_legacy(
    session_factory: async_sessionmaker[AsyncSession], board_fixture: dict
) -> None:
    ids, venues = board_fixture["ids"], board_fixture["venues"]
    params = _stats_params([ids["top"]])
    async with session_factory() as session:
        (legacy,) = await _rows(session, LEGACY_STATS_SQL, params)
        (current,) = await _rows(session, _STATS_SQL.text, params)

    legacy, current = dict(legacy._mapping), dict(current._mapping)
    # array_agg(index_id) has no ORDER BY in either version: compare as sets.
    legacy["scored"] = sorted(legacy["scored"] or [])
    current["scored"] = sorted(current["scored"] or [])
    assert current == legacy

    shown = set(current["venues"])
    assert {venues[n] for n in ("newest", "nested", "top", "edge", "string")} <= shown
    # Only reachable through rows that must not be picked.
    assert not {venues[n] for n in ("middle", "old", "stale")} & shown


@pytest.mark.asyncio
async def test_harvested_market_ids_match_legacy(
    session_factory: async_sessionmaker[AsyncSession], board_fixture: dict
) -> None:
    ids = board_fixture["ids"]
    params = _stats_params([])
    async with session_factory() as session:
        legacy = {r.market_id for r in await _rows(session, LEGACY_MIDS, params)}
        current = {r.market_id for r in await _rows(session, CURRENT_MIDS, params)}

    assert current == legacy
    assert {ids[n] for n in ("newest", "nested", "top", "edge", "string")} <= current
    assert not {ids[n] for n in ("middle", "old", "stale")} & current
    assert not any(m <= 0 for m in current)


@pytest.mark.asyncio
async def test_exact_computed_at_tie_picks_highest_id(
    session_factory: async_sessionmaker[AsyncSession], seeded_data: dict[str, Any]
) -> None:
    """Where DISTINCT ON picked either row of a tie, the rewrite picks the newer id."""
    key = f"{PREFIX}tie"
    at = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        first = TsPageCache(cache_key=key, payload={"n": 1}, computed_at=at)
        session.add(first)
        await session.flush()
        second = TsPageCache(cache_key=key, payload={"n": 2}, computed_at=at)
        session.add(second)
        await session.flush()
        try:
            sql = "WITH RECURSIVE " + _BOARD_LATEST_CTES + " SELECT payload FROM board WHERE cache_key = :key"
            (row,) = await _rows(session, sql, {"key": key})
            assert row.payload == {"n": 2}
            assert second.id > first.id
        finally:
            await session.rollback()
