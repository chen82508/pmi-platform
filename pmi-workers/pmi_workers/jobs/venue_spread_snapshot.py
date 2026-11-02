"""venue-spread-snapshot job — pick the landing page's cross-venue example.

The homepage shows one real question that trades on two exchanges at two
different prices, to make "multi-venue" concrete. Finding that pair means an
O(n^2) cosine self-join over every current index's component markets (~50s over
a few thousand embeddings), so it can't run on the hot path.

What is cached here is the *choice of pair*, not the numbers: the payload holds
market ids, and `/landing/venue-spread` re-reads their prices per request. A
stale snapshot then costs a slightly stale question, never a stale price — the
one thing the card must not get wrong.

Selection: both markets real-money, unresolved, carrying a book worth quoting
(`MIN_VOLUME_24H`), on different venues, inside the SAME index's latest score
(so "both feed this index" is true by construction), embeddings at least
`MIN_COSINE` apart, taking the same side, and naming no conflicting date.
Ranked by price spread, because a pair that agrees illustrates nothing.

Those last two guards are what make ranking by spread safe. Cosine alone can't
tell a venue disagreement from a different question, and the widest gaps are
mostly the latter:

* Deadlines. "Israel and Saudi Arabia normalize relations before 2027?" (13% on
  Polymarket) sits at 0.958 against Kalshi's "…before Jan 20, 2029" (57%) — a
  44-point "disagreement" between two contracts that agree completely. So when
  both titles name years, those years must match; one side naming none is
  allowed (Kalshi lists "Will the Republican party win the governorship in
  Wisconsin" undated against Polymarket's "…in 2026").
* Sides. The two halves of one race embed almost identically, so without the
  polarity check the card could quote "Republicans win Wisconsin" against
  "Democrats win Wisconsin" and call the gap a disagreement — while the two
  prices summing to ~100 means the venues agree exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.models import TsPageCache
from sqlalchemy import insert, text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.venue_spread_snapshot")

CACHE_KEY = "landing:venue-spread"

# A book below this is a quote nobody traded on — showing it next to a $500k
# book would misrepresent how much the disagreement is worth.
MIN_VOLUME_24H = 5_000
# Below peer_grouping's 0.90 default is too loose; above ~0.94 loses genuine
# twins, because the venues word them differently ("Will the Republicans win
# the Wisconsin governor race in 2026?" vs "Will the Republican party win the
# governorship in Wisconsin" — the same contract at 0.934). The date guard,
# not the threshold, is what keeps false twins out.
MIN_COSINE = 0.93

# Indexes to search, and how the landing labels them. Only indexes with a page
# to link to: the card names the index the pair feeds, so it must be reachable.
_INDEXES = [
    ("war-index-2026", "Global War Index", "/micah/war-geo"),
    (
        "us-senate-2026-republican-seats",
        "Senate Index",
        "/micah/balance-of-power?chamber=senate",
    ),
    (
        "us-governor-2026-republican-seats",
        "Governor Index",
        "/micah/balance-of-power?chamber=governor",
    ),
    (
        "us-house-2026-republican-seats",
        "House Index",
        "/micah/balance-of-power?chamber=house",
    ),
    ("btc-bull-bear-index", "Bitcoin Index", "/micah/markets"),
    ("sp500-bull-bear-index", "S&P 500 Index", "/micah/markets?index=sp500"),
]

# `cand` is deliberately narrow before the self-join: the pair search is
# quadratic in its row count, so the volume/real-money/unresolved filters are
# what keep this to seconds rather than minutes.
_SQL = text(
    """
WITH idx(index_id, tag, href) AS (VALUES
    {index_values}
),
def AS (
    SELECT DISTINCT ON (i.index_id) i.index_id, i.tag, i.href, d.id AS def_id
    FROM idx i JOIN core_index_definitions d
      ON d.index_id = i.index_id AND d.is_current
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
           l.index_id, l.tag, l.href, cm.id, cm.venue, cm.title, cm.url,
           cm.last_price,
           -- Every 4-digit year in the title, for the date-conflict guard.
           (SELECT coalesce(array_agg(DISTINCT y[1] ORDER BY y[1]), '{{}}')
              FROM regexp_matches(cm.title, '(\\d{{4}})', 'g') AS y) AS years,
           -- Which side of the question this contract takes. Embeddings put
           -- the two sides of one race very close together ("Will the
           -- Republicans win Wisconsin" vs "Will the Democrats win
           -- Wisconsin"), and quoting those two as one question disagreed on
           -- would invert the meaning of the whole card.
           ARRAY[
               cm.title ~* '(republican|gop)',
               cm.title ~* 'democrat',
               cm.title ~* '(above|over |reach|exceed|higher|greater)',
               cm.title ~* '(below|under |dip|lower|less than)'
           ] AS polarity
    FROM latest l
    JOIN audit_evaluations ae ON ae.id = ANY (l.ids)
    JOIN core_markets cm ON cm.id = ae.market_id
    WHERE cm.resolved_at IS NULL
      AND NOT cm.is_play_money
      AND cm.last_price IS NOT NULL
      AND cm.last_volume_24h >= :min_volume
),
emb AS (
    SELECT DISTINCT ON (v.market_id, v.model) v.market_id, v.model, v.embedding
    FROM vec_market_embeddings v
    WHERE v.market_id IN (SELECT id FROM cand)
    ORDER BY v.market_id, v.model, v.generated_at DESC
),
pairs AS (
    SELECT ca.index_id, ca.tag, ca.href,
           ca.id AS a_id, ca.venue AS a_venue, ca.title AS a_title, ca.url AS a_url,
           cb.id AS b_id, cb.venue AS b_venue, cb.title AS b_title, cb.url AS b_url,
           1 - (ea.embedding <=> eb.embedding) AS cos,
           abs(ca.last_price - cb.last_price) AS spread
    FROM cand ca
    JOIN cand cb
      ON cb.index_id = ca.index_id AND cb.id > ca.id AND cb.venue <> ca.venue
    JOIN emb ea ON ea.market_id = ca.id
    JOIN emb eb ON eb.market_id = cb.id AND eb.model = ea.model
    WHERE 1 - (ea.embedding <=> eb.embedding) >= :min_cos
      AND ca.polarity = cb.polarity
      AND (ca.years = cb.years
           OR cardinality(ca.years) = 0
           OR cardinality(cb.years) = 0)
)
SELECT *, (SELECT count(DISTINCT m) FROM (
              SELECT a_id AS m FROM pairs UNION SELECT b_id FROM pairs
           ) u) AS n_cross_venue
FROM pairs ORDER BY spread DESC, cos DESC LIMIT 1
""".format(
        index_values=",\n    ".join(
            f"('{iid}', '{tag}', '{href}')" for iid, tag, href in _INDEXES
        )
    )
)


@register("venue-spread-snapshot", queue="batch")
async def run() -> dict:
    """Choose the widest real cross-venue price disagreement → ts_page_cache."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        row = (
            await session.execute(
                _SQL, {"min_volume": MIN_VOLUME_24H, "min_cos": MIN_COSINE}
            )
        ).first()

        if row is None:
            # Write the empty payload rather than leaving the last pair up: the
            # card must disappear when nothing qualifies, not keep showing a
            # question that dropped out of the index.
            payload: dict = {"as_of": computed_at.isoformat(), "pair": None}
            log.info("venue_spread_snapshot.no_pair")
        else:
            payload = {
                "as_of": computed_at.isoformat(),
                "n_cross_venue_markets": int(row.n_cross_venue),
                "pair": {
                    "index_id": row.index_id,
                    "tag": row.tag,
                    "href": row.href,
                    "similarity": round(float(row.cos), 4),
                    "markets": [
                        {
                            "market_id": int(row.a_id),
                            "venue": row.a_venue,
                            "title": row.a_title,
                            "url": row.a_url,
                        },
                        {
                            "market_id": int(row.b_id),
                            "venue": row.b_venue,
                            "title": row.b_title,
                            "url": row.b_url,
                        },
                    ],
                },
            }
            log.info(
                "venue_spread_snapshot.picked",
                index_id=row.index_id,
                venues=[row.a_venue, row.b_venue],
                spread_pts=round(float(row.spread) * 100, 1),
                cos=round(float(row.cos), 4),
            )

        await session.execute(
            insert(TsPageCache).values(
                cache_key=CACHE_KEY, payload=payload, computed_at=computed_at
            )
        )
        await session.commit()

    return {"picked": payload.get("pair") is not None}
