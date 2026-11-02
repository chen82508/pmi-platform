"""movers-snapshot job — pre-compute the landing page's "Today's top movers".

Ranking the biggest 24h probability moves needs a per-market current-vs-24h-ago
price diff. Doing that on demand is far too slow (95k active markets → ~78s), so
we scope it to the markets that actually back the four landing indexes and cache
the result hourly for the landing route to read.

Component markets come from each index's own lineage:
  - war / the three seat boards / btc / sp500 are engine indexes → the latest
    `ts_index_scores.component_evaluation_ids` → `audit_evaluations.market_id`.
  - GDP is a title-parser board → the same SQL filter routes/gdp.py uses.
The 24h-ago price is the latest `ts_price_snapshots` row in a bounded 24–30h
window (the (market_id, snapshot_at) index keeps this cheap; ~1s per index).

Near-expiry ladder legs are excluded (`_is_expiring_ladder`): a strike ladder's
"above X" legs mechanically converge to 0/1 as settlement approaches, so on any
given day the same-day S&P legs and the current week's BTC strikes post ±99 pt
"moves" that are calendar mechanics, not the world repricing (2026-09-14: all
12 movers were such legs, and the index cards' "Why it moved" line — fed from
this same cache — explained a falling index with a +98 leg). Daily up-or-down
binaries are the same mechanics without a strike, so they're filtered too.
Ladder legs stay eligible while they are more than `LADDER_CLOSE_WINDOW` from
close; other markets (war, races, GDP) are never excluded, however close to
their deadline — a "by Sep 16" contract surging on news is exactly what the
table is for.

Wide books are excluded too (`_is_wide_book`): the Δ is last trade vs last
trade, and on a book quoted 17 bid / 61 ask one $2.5k print moves it 50 pts
(2026-10-26: kalshi "BTC hits $70,000 before $100,000 — 70,000 first" 13 → 63
led the homepage brief; "S&P 500 above $8,000 before Feb" +66 on 16 / 85 was
next in line). The volume floor can't see this — a single print clears it.
The quote is the market's latest `ts_orderbook_snapshots` row, the one source
both kalshi and polymarket books land in (kalshi's `ts_price_snapshots`
bid/ask are NULL).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.moneyness import extract_strike
from pmi_core.models import TsPageCache
from sqlalchemy import insert, select, text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.movers_snapshot")

CACHE_KEY = "landing:movers"
# The GDP board's snapshot already resolves the real US-GDP membership (title
# REQUIRE/REJECT + is_us(), routes/gdp.py's Python filter). Reuse its cached
# market_ids so movers attributes exactly what the board scores — a local ILIKE
# prefilter here has no is_us() and mislabels foreign GDP (Japan/Mexico/Germany…)
# as "US GDP Index".
GDP_BOARD_KEY = "gdp-board:us-gdp-expectation"
LIMIT = 12
# SQL over-fetch headroom for the Python-side ladder filter. Measured on the
# 2026-09-14 expiry day: 86 of the top 96 rows were excludable legs and the
# 12th real mover sat at rank 98 (272 excludable legs DB-wide, ~3.6k candidates
# in total) — so the headroom must dwarf a full day's ladder listings, not just
# the visible dozen. Extra rows are nearly free: the ranking scan is over all
# candidates regardless of LIMIT.
FETCH_LIMIT = LIMIT * 40

# Liquidity floor for the 24h-mover table: below this a Δ is dust, not a
# repriced book (2026-10-13 report #4/#5 — $0 Illinois/Oregon, $212 West
# Virginia, a $1 Robinhood "BTC < $60K" leg all posted huge "moves").
# core_markets.last_volume_24h is real 24h volume (the lifetime-volume bug was
# fixed 2026-08-10); NULL fails the floor and is excluded, which is what a
# biggest-movers table wants.
MOVERS_MIN_VOL_24H = 1000

# Widest book (ask − bid, 0–1) whose last-trade Δ still counts as a move.
# Measured on the 2026-10-26 candidates: kalshi p50 1 pt / p90 3 pts,
# polymarket max 7 pts — the two phantom movers sat at 44 and 69. Only a quote
# both two-sided and this recent gates; no book (Manifold AMM, gemini,
# forecastex) or a stale one keeps the market, as the volume floor already did.
MOVERS_MAX_SPREAD = 0.15
QUOTE_MAX_AGE = timedelta(hours=24)

# A ladder leg is close enough to settlement that its Δ is expiry convergence,
# not repricing. One week covers the weekly BTC/S&P ladders end to end; the
# longer-dated quarterly strikes stay eligible until their own final week.
LADDER_CLOSE_WINDOW = timedelta(days=7)

# Strike legs `extract_strike` can't see: index-level ladders quote points with
# no $ sign ("above 7509.9999 …", "- 7,510 or above"). Three digits minimum so
# percentage thresholds ("GDP growth above 2.5%") don't read as ladders, and no
# "or more/fewer" so seat-count races don't either.
_THRESHOLD_RE = re.compile(
    r"(?:above|below)\s+[0-9][0-9,]{2,}(?:\.[0-9]+)?"
    r"|[0-9][0-9,]{2,}(?:\.[0-9]+)?\s+or\s+(?:above|below|higher|lower)",
    re.IGNORECASE,
)

# The ladders' strikeless sibling: daily direction binaries ("S&P 500 (SPX)
# Opens Up or Down on September 14?") settle the same day by construction, so
# their ±50 pt "move" is the coin landing, not news.
_UP_OR_DOWN_RE = re.compile(r"\bup or down\b", re.IGNORECASE)


def _is_expiring_ladder(
    title: str | None, closes_at: datetime | None, now: datetime
) -> bool:
    """True when a market is a price-ladder leg (or a daily up-or-down binary)
    inside LADDER_CLOSE_WINDOW of its close (already-closed-but-unresolved
    counts too). Unknown close: keep — only provable expiry mechanics get
    filtered."""
    if closes_at is None:
        return False
    closes = closes_at if closes_at.tzinfo else closes_at.replace(tzinfo=UTC)
    if closes - now > LADDER_CLOSE_WINDOW:
        return False
    if not title:
        return False
    return (
        extract_strike(title) is not None
        or _THRESHOLD_RE.search(title) is not None
        or _UP_OR_DOWN_RE.search(title) is not None
    )


def _is_wide_book(bid, ask, quoted_at: datetime | None, now: datetime) -> bool:
    """True when the market's latest two-sided quote is wider than
    MOVERS_MAX_SPREAD — its last-trade Δ is one print on a thin book, not a
    repricing. A one-sided, missing or stale (> QUOTE_MAX_AGE) quote can't
    prove that, so the market is kept."""
    if bid is None or ask is None or quoted_at is None:
        return False
    at = quoted_at if quoted_at.tzinfo else quoted_at.replace(tzinfo=UTC)
    if now - at > QUOTE_MAX_AGE:
        return False
    return float(ask) - float(bid) > MOVERS_MAX_SPREAD


# Engine indexes → (display tag, tag key, landing href). Their components come
# from audit lineage; GDP is handled separately (SQL title filter).
_ENGINE_INDEXES = [
    ("war-index-2026", "Global War Index", "war", "/micah/war-geo"),
    (
        "us-senate-2026-republican-seats",
        "Senate Index",
        "senate",
        "/micah/balance-of-power?chamber=senate",
    ),
    (
        "us-house-2026-republican-seats",
        "House Index",
        "house",
        "/micah/balance-of-power?chamber=house",
    ),
    (
        "us-governor-2026-republican-seats",
        "Governor Index",
        "governor",
        "/micah/balance-of-power?chamber=governor",
    ),
    # Three boards of the one Markets Index page — tags stay per-board so the
    # movers table still says which reading a contract feeds.
    ("btc-bull-bear-index", "BTC Index", "markets", "/micah/markets"),
    ("sp500-bull-bear-index", "S&P 500 Index", "sp500", "/micah/markets?index=sp500"),
]

_SQL = text(
    """
WITH idx(index_id, tag, tagkey, href) AS (VALUES
    {engine_values}
),
def AS (
    SELECT DISTINCT ON (i.index_id) i.tag, i.tagkey, i.href, d.id AS def_id
    FROM idx i JOIN core_index_definitions d ON d.index_id = i.index_id
    ORDER BY i.index_id, d.version DESC
),
latest AS (
    SELECT def.tag, def.tagkey, def.href, s.component_evaluation_ids AS ids
    FROM def JOIN LATERAL (
        SELECT component_evaluation_ids FROM ts_index_scores ts
        WHERE ts.index_definition_id = def.def_id ORDER BY as_of DESC LIMIT 1
    ) s ON true
),
engine_mids AS (
    SELECT ae.market_id AS mid, l.tag, l.tagkey, l.href
    FROM latest l JOIN audit_evaluations ae ON ae.id = ANY (l.ids)
),
gdp_mids AS (
    -- The GDP board's own resolved US members (is_us() applied), not a re-derived
    -- title filter — so movers can't tag foreign GDP as the US index.
    SELECT unnest(CAST(:gdp_ids AS bigint[])) AS mid,
           'US GDP Index' AS tag, 'economy' AS tagkey,
           '/micah/markets?index=economy' AS href
),
allm AS (
    SELECT DISTINCT ON (mid) mid, tag, tagkey, href
    FROM (SELECT * FROM engine_mids UNION ALL SELECT * FROM gdp_mids) u
    ORDER BY mid
)
SELECT cm.title, cm.url, cm.closes_at, a.tag, a.tagkey, a.href,
       round((cm.last_price - s.p24) * 100, 1) AS delta_pts,
       round(cm.last_price * 100)              AS prob,
       q.best_bid, q.best_ask, q.quoted_at
FROM allm a JOIN core_markets cm ON cm.id = a.mid
JOIN LATERAL (
    SELECT last_price AS p24 FROM ts_price_snapshots ts
    WHERE ts.market_id = cm.id
      AND ts.snapshot_at <= now() - interval '24 hours'
      AND ts.snapshot_at >= now() - interval '30 hours'
    ORDER BY snapshot_at DESC LIMIT 1
) s ON true
LEFT JOIN LATERAL (
    SELECT best_bid, best_ask, snapshot_at AS quoted_at FROM ts_orderbook_snapshots ob
    WHERE ob.market_id = cm.id
    ORDER BY snapshot_at DESC LIMIT 1
) q ON true
WHERE cm.last_price IS NOT NULL AND cm.resolved_at IS NULL AND s.p24 IS NOT NULL
  AND cm.last_volume_24h >= :min_vol
ORDER BY abs(cm.last_price - s.p24) DESC
LIMIT :limit
""".format(
        engine_values=",\n    ".join(
            f"('{iid}', '{tag}', '{key}', '{href}')"
            for iid, tag, key, href in _ENGINE_INDEXES
        )
    )
)


async def _gdp_member_ids(session) -> list[int]:
    """The US-GDP board's resolved member market ids from its latest snapshot.
    Empty (→ no GDP movers this run) if the board hasn't been computed yet —
    better than mislabelling foreign GDP as the US index."""
    row = (
        await session.execute(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == GDP_BOARD_KEY)
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return [int(i) for i in (row or {}).get("market_ids", [])]


@register("movers-snapshot", queue="snapshot")
async def run() -> dict:
    """Rank the landing indexes' component markets by 24h Δ → ts_page_cache."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        gdp_ids = await _gdp_member_ids(session)
        rows = (
            await session.execute(
                _SQL,
                {"limit": FETCH_LIMIT, "gdp_ids": gdp_ids, "min_vol": MOVERS_MIN_VOL_24H},
            )
        ).all()
        not_ladder = [
            r for r in rows if not _is_expiring_ladder(r.title, r.closes_at, computed_at)
        ]
        kept = [
            r
            for r in not_ladder
            if not _is_wide_book(r.best_bid, r.best_ask, r.quoted_at, computed_at)
        ]
        movers = [
            {
                "title": r.title,
                "url": r.url,
                "tag": r.tag,
                "tagkey": r.tagkey,
                "href": r.href,
                "delta_pts": float(r.delta_pts),
                "prob": int(r.prob),
            }
            for r in kept[:LIMIT]
        ]
        payload = {"as_of": computed_at.isoformat(), "movers": movers}

        await session.execute(
            insert(TsPageCache).values(
                cache_key=CACHE_KEY, payload=payload, computed_at=computed_at
            )
        )
        await session.commit()

    log.info(
        "movers_snapshot.done",
        n=len(movers),
        excluded_expiring_ladders=len(rows) - len(not_ladder),
        excluded_wide_books=len(not_ladder) - len(kept),
        computed_at=computed_at.isoformat(),
    )
    return {
        "n_movers": len(movers),
        "excluded_expiring_ladders": len(rows) - len(not_ladder),
        "excluded_wide_books": len(not_ladder) - len(kept),
    }
