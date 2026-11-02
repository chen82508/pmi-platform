"""Landing-page read API.

* `/movers` — ranking the landing indexes' component markets by 24h Δ
  (`movers-snapshot`). The cache holds the finished rows.
* `/venue-spread` — finding one question two exchanges price differently
  (`venue-spread-snapshot`, an O(n^2) cosine join). The cache holds only the
  CHOICE of pair; the prices below are read live, so an old snapshot costs a
  stale question rather than a stale price.
* `/stats` — the hero's headline counts, computed live off the readings'
  lineage (one indexed join, no snapshot needed).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Depends
from pmi_core.models import CoreMarket
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest

router = APIRouter(prefix="/landing", tags=["landing"])

CACHE_KEY = "landing:movers"
VENUE_SPREAD_KEY = "landing:venue-spread"
GDP_BOARD_KEY = "gdp-board:us-gdp-expectation"

# The index definitions behind the landing's cards, which are the site's
# published indexes. `semantic-war-demo` is current but unpublished, and the
# MAGA definitions still score (the seat boards are built from them) but are no
# longer published, so this can't be "every is_current row".
PUBLISHED_INDEX_IDS = [
    "war-index-2026",
    "us-senate-2026-republican-seats",
    "us-house-2026-republican-seats",
    "us-governor-2026-republican-seats",
    "btc-bull-bear-index",
    "sp500-bull-bear-index",
]

# Distinct contracts + venues actually ON DISPLAY across every vertical, so the
# hero's "N exchanges" auto-syncs with the real site state instead of a curated
# list. Two sources, unioned:
#   1. Scored index readings' lineage (`component_evaluation_ids`) — war, btc,
#      sp500, seats. Describes the readings that exist, not what
#      the definitions permit.
#   2. Every fresh board snapshot in `ts_page_cache` — the computed verticals
#      (gdp, climate, markets, …) have no engine lineage, so their venues
#      (gemini + robinhood live ONLY in Climate City Heat) were invisible to a
#      lineage-only count. We harvest every `market_id` at ANY nesting depth via
#      the `$.**` recursive wildcard, so a NEW vertical is counted automatically
#      the moment its board writes a payload — no code change, no hardcoded list.
# `computed_at > now() - 2 days` keeps it a live figure: a retired vertical's
# stale cache drops out on its own. DISTINCT is the whole point — indexes can
# read the same contracts; summing cards would double-count.
# Synthetic ids (senate/house/governor board rows carry market_id = -(i+1) to key
# a state race) are dropped by `market_id > 0`.
#
# `board` is each cache_key's newest row inside that window, found by a skip
# scan: `board_keys` walks ix_ts_page_cache__key_time one distinct key at a time
# (an index probe per key), then LATERAL takes the key's newest row in the window,
# or none for a key whose snapshots have all aged out. It selects the same rows as
# `DISTINCT ON (cache_key) ... ORDER BY cache_key, computed_at DESC`, which
# fetched every window row from the heap (~91k, to keep ~1.3k) and took minutes on
# a saturated disk. `id DESC` only breaks exact computed_at ties, where DISTINCT
# ON picked arbitrarily. Kept as its own string so the parity tests
# (tests/test_landing_stats_parity.py) can compare the key selection alone.
_BOARD_LATEST_CTES = """
board_keys AS (
    (SELECT cache_key FROM ts_page_cache ORDER BY cache_key LIMIT 1)
    UNION ALL
    SELECT (SELECT t.cache_key FROM ts_page_cache t
            WHERE t.cache_key > k.cache_key
            ORDER BY t.cache_key LIMIT 1)
    FROM board_keys k
    WHERE k.cache_key IS NOT NULL
),
board AS (
    SELECT b.cache_key, b.computed_at, b.payload
    FROM board_keys k
    CROSS JOIN LATERAL (
        SELECT t.cache_key, t.computed_at, t.payload
        FROM ts_page_cache t
        WHERE t.cache_key = k.cache_key
          AND t.computed_at > now() - interval '2 days'
        ORDER BY t.computed_at DESC, t.id DESC
        LIMIT 1
    ) b
)
"""

_STATS_SQL = text(
    """
WITH RECURSIVE latest AS (
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
"""
    + _BOARD_LATEST_CTES
    + """,
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
)


# Where each published index's drill-down readings live. A "reading" here is
# anything on the site with its own score: a country or region under the war
# index, a seat under a chamber, a question under a markets
# index, a period under the economy board.
CYCLE_YEAR = 2026
READING_KEYS: dict[str, str] = {
    "war": f"war:{CYCLE_YEAR}",
    "senate": "senate-board:us-senate-2026-republican-seats",
    "house": "senate-board:us-house-2026-republican-seats",
    "governor": "senate-board:us-governor-2026-republican-seats",
    "btc": "markets-board:btc-bull-bear-index",
    "sp500": "markets-board:sp500-bull-bear-index",
    "gdp": GDP_BOARD_KEY,
}


def count_sub_readings(payloads: dict[str, dict | None]) -> int:
    """Scored drill-down readings across the published indexes' snapshots.

    Only entries carrying a score count, so a country with no priced contract
    or a question whose strikes all lack a price is not claimed as an index.
    """
    total = 0
    war = payloads.get("war") or {}
    total += sum(1 for c in (war.get("countries") or {}).values() if c.get("heat") is not None)
    total += sum(1 for r in (war.get("regions") or {}).values() if r.get("heat") is not None)
    for chamber in ("senate", "house", "governor"):
        board = payloads.get(chamber) or {}
        total += sum(1 for r in board.get("races") or [] if r.get("prob_r") is not None)
    for market in ("btc", "sp500"):
        board = payloads.get(market) or {}
        total += len({
            r["anchor"] for r in board.get("rows") or []
            if r.get("anchor") and r.get("prob") is not None
        })
    gdp = payloads.get("gdp") or {}
    total += sum(1 for a in gdp.get("anchors") or [] if a.get("sentiment") is not None)
    return total


def _universe_estimate(raw: float | None) -> int | None:
    """Size of the corpus market selection runs over, or None if unknown.

    `pg_class.reltuples` is -1 on a table autovacuum has never analyzed, so
    anything non-positive is the absence of an estimate rather than a count of
    zero. /methodology drops its "Universe" row instead of quoting one.
    """
    if raw is None or raw <= 0:
        return None
    return int(raw)


@router.get("/movers")
async def movers(session: AsyncSession = Depends(get_session)) -> dict:
    """Top 24h movers across the landing indexes (from the hourly snapshot)."""
    payload = await read_latest(session, CACHE_KEY)
    rows = (payload or {}).get("movers", [])
    return {
        "summary": (
            f"{len(rows)} top movers as of {(payload or {}).get('as_of')}"
            if rows
            else "top movers not computed yet"
        ),
        "data": {"movers": rows, "as_of": (payload or {}).get("as_of")},
    }


@router.get("/home")
async def home(session: AsyncSession = Depends(get_session)) -> dict | None:
    """The homepage's pre-assembled landing data (cards/movers/stats), written by
    the `home-snapshot` worker into ts_page_cache every ~10 min. Returns the cached
    payload as-is; None when cold → the web app falls back to a live fan-out. Serves
    the homepage from one cached read instead of its ~12-endpoint fan-out."""
    return await read_latest(session, "landing-home")


def _parse_ts(value: str | None) -> datetime | None:
    """Cache timestamps are JSON strings, and the snapshot writers spell UTC
    both ways ('…Z' and '…+00:00'). Compare them as instants, not text."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# The hero stats are near-static (venues change only when a new venue's markets
# first enter a vertical — rare) but `_STATS_SQL`'s board harvest walks ~1.3k
# board payloads with a recursive jsonb path and costs several seconds. /stats is
# on the hot path of every landing + /methodology render, so we serve a cached
# result and only recompute past the TTL. A lock collapses a cold-cache thundering
# herd to a single query; everyone else gets the cached dict.
_STATS_TTL_S = 1800.0
_stats_cache: dict = {"at": 0.0, "data": None}
_stats_lock = asyncio.Lock()


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    """Cached wrapper over `_compute_stats` — see it for what the figures mean."""
    now = time.monotonic()
    if _stats_cache["data"] is not None and now - _stats_cache["at"] < _STATS_TTL_S:
        return _stats_cache["data"]
    async with _stats_lock:
        # Another request may have refreshed the cache while we waited on the lock.
        now = time.monotonic()
        if _stats_cache["data"] is not None and now - _stats_cache["at"] < _STATS_TTL_S:
            return _stats_cache["data"]
        data = await _compute_stats(session)
        _stats_cache["data"] = data
        _stats_cache["at"] = time.monotonic()
        return data


async def _compute_stats(session: AsyncSession) -> dict:
    """What the landing hero counts: live indexes, contracts, venues, freshness.

    The hero figures are properties of what is on display today, not of the
    catalogue: an index only counts as live if its newest reading has a score,
    and the contract and venue counts come from everything actually shown —
    the scored readings' lineage UNION every fresh board snapshot in
    `ts_page_cache`. Computed verticals (National Economy, Climate, …) have no
    engine lineage, so their venues are recovered from the market ids their
    board payloads carry; see `_STATS_SQL` for the recursive harvest.

    ``universe`` is the one catalogue figure, for /methodology: how many markets
    step 1 selects from. It is deliberately NOT ``contracts`` — quoting the few
    thousand contracts that made it into the readings as the size of the pool
    they were chosen from would understate the selection by three orders of
    magnitude.
    """
    snapshots = {k: await read_latest(session, key) for k, key in READING_KEYS.items()}
    gdp = snapshots["gdp"]
    gdp_ids = [int(i) for i in (gdp or {}).get("market_ids", [])]
    row = (
        await session.execute(
            _STATS_SQL, {"index_ids": PUBLISHED_INDEX_IDS, "gdp_ids": gdp_ids}
        )
    ).one()

    # Definitions → cards: the economy board is a card with no definition at all.
    live = set(row.scored or [])
    freshest = row.as_of
    if (gdp or {}).get("score") is not None:
        live.add("us-gdp-expectation")
        gdp_at = _parse_ts((gdp or {}).get("as_of"))
        if gdp_at and (freshest is None or gdp_at > freshest):
            freshest = gdp_at

    return {
        "summary": (
            f"{len(live)} live indexes across {row.contracts} contracts "
            f"on {len(row.venues or [])} venues"
        ),
        "data": {
            "indexes": len(live),
            "contracts": row.contracts,
            "venues": list(row.venues or []),
            # Every scored reading a visitor can open: the index cards plus
            # their drill-downs. `indexes` stays the card count.
            "readings": len(live) + count_sub_readings(snapshots),
            "universe": _universe_estimate(row.universe),
            "as_of": freshest.isoformat() if freshest else None,
        },
    }


def _empty(reason: str) -> dict:
    return {"summary": reason, "data": {"pair": None}}


@router.get("/venue-spread")
async def venue_spread(session: AsyncSession = Depends(get_session)) -> dict:
    """One real question, priced by two exchanges, with live prices.

    Returns ``pair: null`` — and the landing then omits the whole block —
    whenever the snapshot is missing, found nothing, or either side has since
    resolved or lost its price. There is no fallback example to fall back to:
    the illustration is either today's real disagreement or it is absent.
    """
    payload = await read_latest(session, VENUE_SPREAD_KEY)
    pair = (payload or {}).get("pair")
    if not pair:
        return _empty("no cross-venue pair computed yet")

    cached = pair.get("markets") or []
    ids = [m["market_id"] for m in cached]
    rows = {
        m.id: m
        for m in (
            await session.execute(select(CoreMarket).where(CoreMarket.id.in_(ids)))
        ).scalars()
    }

    quotes = []
    for m in cached:
        row = rows.get(m["market_id"])
        if row is None or row.resolved_at is not None or row.last_price is None:
            return _empty("cross-venue pair no longer quotable")
        quotes.append(
            {
                "venue": m["venue"],
                "title": m["title"],
                # Prefer the market's own current url; the snapshot's copy is a
                # fallback for rows ingested before core_markets.url existed.
                "url": row.url or m.get("url"),
                "prob": round(float(row.last_price) * 100, 1),
                "volume_24h": (
                    float(row.last_volume_24h)
                    if row.last_volume_24h is not None
                    else None
                ),
            }
        )
    # Deepest book first: it leads the sentence, and it carries the most weight
    # in the index the pair feeds.
    quotes.sort(key=lambda q: q["volume_24h"] or 0, reverse=True)

    spread = round(abs(quotes[0]["prob"] - quotes[1]["prob"]), 1)
    return {
        "summary": (
            f"{quotes[0]['venue']} {quotes[0]['prob']}% vs "
            f"{quotes[1]['venue']} {quotes[1]['prob']}% — {spread} pts apart"
        ),
        "data": {
            "pair": {
                # No score here on purpose: the pair belongs to one index
                # definition (e.g. us-governor-2026-republican-seats) while the
                # landing card above it shows the family reading.
                # Printing the sub-index's number beside it would read as a
                # contradiction.
                "index": {
                    "id": pair["index_id"],
                    "tag": pair["tag"],
                    "href": pair["href"],
                },
                "similarity": pair.get("similarity"),
                "spread_pts": spread,
                "quotes": quotes,
                "n_cross_venue_markets": (payload or {}).get("n_cross_venue_markets"),
                "as_of": (payload or {}).get("as_of"),
            }
        },
    }
