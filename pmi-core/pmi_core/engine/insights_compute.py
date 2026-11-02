"""Insights vertical compute — the weekly editorial reading, fully derived.

`/micah/insights` is a derived product, not a CMS: every section is computed from
the same data plane the rest of the platform reads, then FROZEN into one issue so
the page and (later) the emailed weekly report render the identical snapshot.

Five sections, each an engine here:

* ``chamber_spread`` — the lead. The gap between the widest and narrowest chamber
  lean (MAGA senate/house/governor), its 12-week weekly series, where the current
  gap sits against that band, and the chamber that moved it most over 7 days.
* ``gap_pairs`` — "where the exchanges disagree": the widest real cross-venue
  price disagreements on the same question (reuses the venue-spread self-join,
  LIMIT N + a funnel count).
* ``settling_this_week`` — priced contracts owned by a tracked index that close in
  the next 7 days, with the index they feed and (for races) the state.
* ``every_reading`` — one line per published reading: its value, 7-day delta, and
  position inside its OWN 12-week range.
* the issue envelope (``compute_insights``) — date, ISO week, and the four
  sections; the snapshot job freezes it under a retention-exempt archive key.

Honest-data: a section that has no real data is returned empty (the page hides
it); nothing is synthesized. Windows are real (12 weeks of ts_index_scores /
ts_page_cache history), never back-filled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One line per published reading. (index_id or cache-key, label, href, unit,
# source) — "score" reads ts_index_scores (YAML indexes), "cache" reads a
# ts_page_cache key's own `score` history (computed verticals).
_READINGS: list[tuple[str, str, str, str, str]] = [
    ("war-index-2026", "Global War Index", "/micah/war-geo", "PMI", "score"),
    ("us-senate-2026-republican-seats", "Senate · R seats", "/micah/balance-of-power?chamber=senate", "seats", "score"),
    ("us-house-2026-republican-seats", "House · R seats", "/micah/balance-of-power?chamber=house", "seats", "score"),
    ("us-governor-2026-republican-seats", "Governor · R seats", "/micah/balance-of-power?chamber=governor", "seats", "score"),
    ("btc-bull-bear-index", "Bitcoin Bull–Bear", "/micah/markets", "PMI", "score"),
    ("sp500-bull-bear-index", "S&P 500 Bull–Bear", "/micah/markets?index=sp500", "PMI", "score"),
    ("gdp-board:us-gdp-expectation", "US GDP Expectation", "/micah/gdp", "PMI", "cache"),
    ("climate-hub", "Climate & Weather", "/micah/climate", "PMI", "cache"),
]

# Tracked indexes for the settling / gap sections (id, tag, href) — the ones with
# a page to link to. Matches the movers / venue-spread catalogs.
_TRACKED: list[tuple[str, str, str]] = [
    ("war-index-2026", "Global War Index", "/micah/war-geo"),
    ("us-senate-2026-republican-seats", "Senate Index", "/micah/balance-of-power?chamber=senate"),
    ("us-house-2026-republican-seats", "House Index", "/micah/balance-of-power?chamber=house"),
    ("us-governor-2026-republican-seats", "Governor Index", "/micah/balance-of-power?chamber=governor"),
    ("btc-bull-bear-index", "Bitcoin Index", "/micah/markets"),
    ("sp500-bull-bear-index", "S&P 500 Index", "/micah/markets?index=sp500"),
]

_CHAMBERS = [
    ("maga-senate-2026", "Senate"),
    ("maga-house-2026", "House"),
    ("maga-governor-2026", "Governor"),
]


async def _score_history(session: AsyncSession, index_id: str, days: int) -> list[tuple[datetime, float]]:
    """[(as_of, score)] ascending for a YAML index over the last `days`."""
    rows = (
        await session.execute(
            text(
                """
                SELECT s.as_of, s.score
                FROM ts_index_scores s
                JOIN core_index_definitions d ON d.id = s.index_definition_id
                WHERE d.index_id = :iid AND d.is_current
                  AND s.as_of >= :start AND s.score IS NOT NULL
                ORDER BY s.as_of
                """
            ),
            {"iid": index_id, "start": datetime.now(UTC) - timedelta(days=days)},
        )
    ).all()
    return [(r.as_of, float(r.score)) for r in rows]


async def _cache_history(session: AsyncSession, cache_key: str, days: int) -> list[tuple[datetime, float]]:
    """[(computed_at, score)] ascending for a computed-vertical cache key."""
    rows = (
        await session.execute(
            text(
                """
                SELECT computed_at, (payload->>'score')::numeric AS score
                FROM ts_page_cache
                WHERE cache_key = :k AND computed_at >= :start
                  AND payload->>'score' IS NOT NULL
                ORDER BY computed_at
                """
            ),
            {"k": cache_key, "start": datetime.now(UTC) - timedelta(days=days)},
        )
    ).all()
    return [(r.computed_at, float(r.score)) for r in rows]


def _delta_before(points: list[tuple[datetime, float]], days: int) -> float | None:
    """value(now) - value(≈`days` ago), using the last point at/ before the mark."""
    if len(points) < 2:
        return None
    mark = points[-1][0] - timedelta(days=days)
    prior = [v for t, v in points if t <= mark]
    if not prior:
        return None
    return round(points[-1][1] - prior[-1], 2)


def _week_key(t: datetime) -> str:
    iso = t.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


async def chamber_spread(session: AsyncSession, weeks: int = 12) -> dict | None:
    """Lead story: the max−min gap across the three MAGA chamber leans, its weekly
    series over `weeks`, its position in that band, and the biggest 7d mover."""
    days = weeks * 7
    hist = {cid: await _score_history(session, cid, days) for cid, _ in _CHAMBERS}
    if any(len(h) < 2 for h in hist.values()):
        return None

    # Weekly spread: for each ISO week, the last score of each chamber that week,
    # then max−min. Only weeks where all three chambers have a point.
    weekly: dict[str, dict[str, float]] = {}
    for cid, _ in _CHAMBERS:
        for t, v in hist[cid]:
            weekly.setdefault(_week_key(t), {})[cid] = v  # ascending → last wins
    # Only weeks where all three chambers have a point, so the gap is a real gap.
    series = [
        {"week": wk, "spread": round(max(vals.values()) - min(vals.values()), 2)}
        for wk in sorted(weekly)
        if len(vals := weekly[wk]) == len(_CHAMBERS)
    ]
    if len(series) < 2:
        return None

    now_vals = {cid: hist[cid][-1][1] for cid, _ in _CHAMBERS}
    hi_cid = max(now_vals, key=now_vals.get)
    lo_cid = min(now_vals, key=now_vals.get)
    spread_now = round(now_vals[hi_cid] - now_vals[lo_cid], 2)
    label = {cid: lbl for cid, lbl in _CHAMBERS}
    spreads = [s["spread"] for s in series]
    band_lo, band_hi = round(min(spreads), 1), round(max(spreads), 1)

    # Band position: is the current gap the widest / among the widest / normal?
    # Wording stays honest about the REAL span — the current def's history is only
    # as long as it is (a version bump resets it), so we never claim "12-week".
    prior_max = max(spreads[:-1]) if len(spreads) > 1 else spreads[0]
    if spread_now >= prior_max:
        band = "the widest gap on record"
    elif spread_now >= band_lo + 0.75 * (band_hi - band_lo):
        band = "near the top of its range"
    elif spread_now <= band_lo + 0.25 * (band_hi - band_lo):
        band = "near the bottom of its range"
    else:
        band = "mid-range"

    # Biggest 7d mover among the three chambers (the "largest contributor").
    movers = []
    for cid, lbl in _CHAMBERS:
        d = _delta_before(hist[cid], 7)
        if d is not None:
            movers.append((lbl, d))
    top_mover = max(movers, key=lambda m: abs(m[1])) if movers else None

    return {
        "spread_now": spread_now,
        "high": {"chamber": label[hi_cid], "value": round(now_vals[hi_cid], 1)},
        "low": {"chamber": label[lo_cid], "value": round(now_vals[lo_cid], 1)},
        "band": band,
        "band_lo": band_lo,
        "band_hi": band_hi,
        "series": series,
        "weeks": len(series),
        "top_mover": ({"chamber": top_mover[0], "delta_7d": top_mover[1]} if top_mover else None),
    }


# Reuse the venue-spread self-join, but return the top N pairs (not 1) plus the
# funnel count. `{index_values}` is filled from _TRACKED at call time.
_GAP_SQL = """
WITH idx(index_id, tag, href) AS (VALUES {index_values}),
def AS (
    SELECT DISTINCT ON (i.index_id) i.index_id, i.tag, i.href, d.id AS def_id
    FROM idx i JOIN core_index_definitions d ON d.index_id = i.index_id AND d.is_current
    ORDER BY i.index_id, d.version DESC
),
latest AS (
    SELECT def.index_id, def.tag, def.href, s.component_evaluation_ids AS ids
    FROM def JOIN LATERAL (
        SELECT component_evaluation_ids FROM ts_index_scores ts
        WHERE ts.index_definition_id = def.def_id ORDER BY as_of DESC LIMIT 1
    ) s ON true
),
cand AS (
    SELECT DISTINCT ON (l.index_id, ae.market_id)
           l.index_id, l.tag, l.href, cm.id, cm.venue, cm.title, cm.url, cm.last_price,
           (SELECT coalesce(array_agg(DISTINCT y[1] ORDER BY y[1]), '{{}}')
              FROM regexp_matches(cm.title, '(\\d{{4}})', 'g') AS y) AS years,
           ARRAY[
               cm.title ~* '(republican|gop)', cm.title ~* 'democrat',
               cm.title ~* '(above|over |reach|exceed|higher|greater)',
               cm.title ~* '(below|under |dip|lower|less than)'
           ] AS polarity
    FROM latest l
    JOIN audit_evaluations ae ON ae.id = ANY (l.ids)
    JOIN core_markets cm ON cm.id = ae.market_id
    WHERE cm.resolved_at IS NULL AND NOT cm.is_play_money
      AND cm.last_price IS NOT NULL AND cm.last_volume_24h >= :min_volume
),
emb AS (
    SELECT DISTINCT ON (v.market_id, v.model) v.market_id, v.model, v.embedding
    FROM vec_market_embeddings v WHERE v.market_id IN (SELECT id FROM cand)
    ORDER BY v.market_id, v.model, v.generated_at DESC
),
pairs AS (
    SELECT ca.index_id, ca.tag, ca.href,
           ca.id AS a_id, ca.venue AS a_venue, ca.title AS a_title, ca.url AS a_url, ca.last_price AS a_price,
           cb.id AS b_id, cb.venue AS b_venue, cb.title AS b_title, cb.url AS b_url, cb.last_price AS b_price,
           1 - (ea.embedding <=> eb.embedding) AS cos,
           abs(ca.last_price - cb.last_price) AS spread
    FROM cand ca
    JOIN cand cb ON cb.index_id = ca.index_id AND cb.id > ca.id AND cb.venue <> ca.venue
    JOIN emb ea ON ea.market_id = ca.id
    JOIN emb eb ON eb.market_id = cb.id AND eb.model = ea.model
    WHERE 1 - (ea.embedding <=> eb.embedding) >= :min_cos
      AND ca.polarity = cb.polarity
      AND (ca.years = cb.years OR cardinality(ca.years) = 0 OR cardinality(cb.years) = 0)
)
SELECT *, (SELECT count(DISTINCT m) FROM (
              SELECT a_id AS m FROM pairs UNION SELECT b_id FROM pairs) u) AS n_cross_venue
FROM pairs ORDER BY spread DESC, cos DESC LIMIT :limit
"""

_MIN_VOLUME_24H = 5_000
_MIN_COSINE = 0.93


async def gap_pairs(session: AsyncSession, limit: int = 6) -> dict:
    """Top `limit` cross-venue price disagreements on the same question."""
    sql = text(
        _GAP_SQL.format(
            index_values=",\n    ".join(f"('{i}', '{t}', '{h}')" for i, t, h in _TRACKED)
        )
    )
    rows = (
        await session.execute(
            sql, {"min_volume": _MIN_VOLUME_24H, "min_cos": _MIN_COSINE, "limit": limit}
        )
    ).all()
    pairs = []
    for r in rows:
        # Present the higher-priced leg first so the gap reads left-to-right.
        legs = sorted(
            [
                {"venue": r.a_venue, "title": r.a_title, "url": r.a_url, "price": round(float(r.a_price) * 100, 1)},
                {"venue": r.b_venue, "title": r.b_title, "url": r.b_url, "price": round(float(r.b_price) * 100, 1)},
            ],
            key=lambda m: m["price"],
            reverse=True,
        )
        pairs.append(
            {
                "index_id": r.index_id,
                "tag": r.tag,
                "href": r.href,
                "spread": round(float(r.spread) * 100, 1),
                "similarity": round(float(r.cos), 4),
                "legs": legs,
            }
        )
    return {"pairs": pairs, "n_cross_venue_markets": int(rows[0].n_cross_venue) if rows else 0}


_SETTLING_SQL = """
WITH idx(index_id, tag, href) AS (VALUES {index_values}),
def AS (
    SELECT DISTINCT ON (i.index_id) i.index_id, i.tag, i.href, d.id AS def_id
    FROM idx i JOIN core_index_definitions d ON d.index_id = i.index_id AND d.is_current
    ORDER BY i.index_id, d.version DESC
),
latest AS (
    SELECT def.index_id, def.tag, def.href, s.component_evaluation_ids AS ids
    FROM def JOIN LATERAL (
        SELECT component_evaluation_ids FROM ts_index_scores ts
        WHERE ts.index_definition_id = def.def_id ORDER BY as_of DESC LIMIT 1
    ) s ON true
),
mk AS (
    SELECT DISTINCT ON (cm.id) cm.id, cm.title, cm.venue, cm.url, cm.last_price,
           cm.last_volume_24h, cm.closes_at, l.tag, l.href, rt.state
    FROM latest l
    JOIN audit_evaluations ae ON ae.id = ANY (l.ids)
    JOIN core_markets cm ON cm.id = ae.market_id
    LEFT JOIN core_market_race_tag rt ON rt.market_id = cm.id
    WHERE cm.resolved_at IS NULL AND NOT cm.is_play_money
      AND cm.last_price IS NOT NULL
      AND cm.closes_at IS NOT NULL
      AND cm.closes_at >= :now AND cm.closes_at < :week_end
),
-- Cap per index so a vertical with many same-day settlements (BTC price ladders)
-- doesn't crowd out everything else — the list should show what's settling ACROSS
-- the indexes this week, most-traded first within each.
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY tag ORDER BY last_volume_24h DESC NULLS LAST, closes_at) AS rn
    FROM mk
)
SELECT * FROM ranked WHERE rn <= 6 ORDER BY closes_at, tag LIMIT :limit
"""


async def settling_this_week(session: AsyncSession, limit: int = 24) -> dict:
    """Priced, tracked-index contracts that close in the next 7 days."""
    now = datetime.now(UTC)
    sql = text(
        _SETTLING_SQL.format(
            index_values=",\n    ".join(f"('{i}', '{t}', '{h}')" for i, t, h in _TRACKED)
        )
    )
    rows = (
        await session.execute(
            sql, {"now": now, "week_end": now + timedelta(days=7), "limit": limit}
        )
    ).all()
    items = [
        {
            "title": r.title,
            "venue": r.venue,
            "url": r.url,
            "tag": r.tag,
            "href": r.href,
            "state": r.state,
            "price": round(float(r.last_price) * 100, 1),
            "closes_at": r.closes_at.isoformat(),
        }
        for r in rows
    ]
    return {"window": {"from": now.isoformat(), "to": (now + timedelta(days=7)).isoformat()}, "items": items}


async def every_reading(session: AsyncSession, weeks: int = 12) -> list[dict]:
    """One line per published reading: value, 7d delta, 12-week range position."""
    days = weeks * 7
    out: list[dict] = []
    for key, label, href, unit, source in _READINGS:
        pts = (
            await _score_history(session, key, days)
            if source == "score"
            else await _cache_history(session, key, days)
        )
        if not pts:
            continue
        value = round(pts[-1][1], 2)
        vals = [v for _, v in pts]
        lo, hi = round(min(vals), 2), round(max(vals), 2)
        pos = None if hi <= lo else round((value - lo) / (hi - lo), 3)
        span_days = int((pts[-1][0] - pts[0][0]).total_seconds() // 86400)
        out.append(
            {
                "label": label,
                "href": href,
                "unit": unit,
                "value": value,
                "delta_7d": _delta_before(pts, 7),
                "range_lo": lo,
                "range_hi": hi,
                "range_pos": pos,
                "weeks": weeks,
                "span_days": span_days,  # real coverage — a recent def bump makes this < weeks*7
            }
        )
    return out


async def compute_insights(session: AsyncSession) -> dict:
    """The full frozen issue payload for one week."""
    now = datetime.now(UTC)
    lead = await chamber_spread(session)
    gaps = await gap_pairs(session)
    settling = await settling_this_week(session)
    readings = await every_reading(session)
    return {
        "issue_date": now.date().isoformat(),
        "iso_week": _week_key(now),
        "computed_at": now.isoformat(),
        "lead": lead,
        "gap_pairs": gaps,
        "settling": settling,
        "readings": readings,
    }
