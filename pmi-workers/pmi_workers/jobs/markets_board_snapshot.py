"""markets-board-snapshot job — pre-compute the BTC markets board into
ts_page_cache, off the request hot path.

Same pattern as war/senate/house/governor/gdp board snapshots: run the heavy
computation every 10 min and stash the rendered payload in ts_page_cache so the
pmi-api route serves it in a single latest-wins read.

Why this exists: the /micah/markets board live-computes a board over its
component set + price-snapshot scans. That was fast against the old standalone
`pmi_btc` snapshot DB (≤2.2k markets), but that DB was ephemeral and vanished
during an EC2 rebuild → 500s. Live-computing the same board over the main `pmi`
DB (~1M markets) times out (>120s), so BTC — like every other vertical — now
serves from a cached snapshot computed here against the MAIN `pmi` DB. No second
DB, no `pmi-api-btc`.

The compute lives in `pmi_core.engine.markets_board_compute.compute_markets_board`
so this worker and the pmi-api fallback share one implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine.anchor_trend import compute_anchor_trends
from pmi_core.engine.markets_board_compute import compute_markets_board
from pmi_core.models import TsPageCache
from sqlalchemy import insert

from pmi_workers.registry import register
from pmi_workers.spot_feed import fetch_btc_spot, fetch_sp500_spot

# Per-index external spot source (baked into the snapshot). Indexes not listed
# fall back to the ladder-implied spot (None → the source isn't fetched).
_SPOT_FETCHERS = {
    "btc-bull-bear-index": fetch_btc_spot,
    "sp500-bull-bear-index": fetch_sp500_spot,
}

log = structlog.get_logger("pmi_workers.jobs.markets_board_snapshot")

INDEX_ID = "btc-bull-bear-index"
CACHE_KEY = f"markets-board:{INDEX_ID}"


@register("markets-board-snapshot", queue="snapshot")
async def run(index_id: str = INDEX_ID) -> dict:
    """Compute the markets board for `index_id` and write it to ts_page_cache."""
    computed_at = datetime.now(UTC)
    cache_key = f"markets-board:{index_id}"

    # Bake in a live external spot (fallback chain) so the board's distribution
    # anchor readings/trends land on the bull/bear axis: BTC/USD from exchanges,
    # the S&P 500 (SPX) index level from Yahoo/Stooq. Fetched HERE (the cron),
    # once per snapshot — never on the request path. On total failure spot is
    # None → compute falls back to the ladder-implied spot (None for S&P → its
    # distribution anchors stay "—", same as before).
    fetcher = _SPOT_FETCHERS.get(index_id)
    spot = await fetcher() if fetcher else None

    async with session_scope() as session:
        payload = await compute_markets_board(session, index_id, as_of=None, spot_override=spot)
        if payload is None:
            log.warning("markets_board_snapshot.no_score", index_id=index_id)
            return {"error": "no score yet — run the pipeline first"}

        await session.execute(
            insert(TsPageCache).values(
                cache_key=cache_key,
                payload=payload,
                computed_at=computed_at,
            )
        )

        # Pre-compute each anchor's By-Question trend into its own key so the
        # detail page reads it instead of scanning ts_price_snapshots per request
        # (the last live-compute residual). Same snapshot, same spot as the board.
        # 30d, not 90d: the 90d ts_price_snapshots scan across hundreds of BTC/SP500
        # member markets runs past this board snapshot's 10-min batch-lane cadence
        # (measured; the adaptive _member_hourly trimmed the payload but not the
        # scan). This trend gives the 7/30D chips their backfilled hourly detail.
        trends = await compute_anchor_trends(
            session, payload.get("rows", []), payload.get("implied_spot"), days=30
        )
        anchor_readings = 0
        for anchor, trend in trends.items():
            session.add(
                TsPageCache(
                    cache_key=f"markets-trend:{index_id}:{anchor}",
                    payload=trend,
                    computed_at=computed_at,
                )
            )
            # Scalar-cache pipeline (calendar-gated) for the 90D chip: persist this
            # anchor's CURRENT reading (the trend's last point == the tile) as one
            # tiny row per snapshot. Its own ts_page_cache history then IS the long
            # trend — read cheaply via scalar_points, no member-price scan — so the
            # 90D chip lights up as coverage accrues. retention keeps markets-anchor:*.
            reading = next((v for v in reversed(trend.get("data") or []) if v is not None), None)
            if reading is not None:
                session.add(
                    TsPageCache(
                        cache_key=f"markets-anchor:{index_id}:{anchor}",
                        payload={"reading": reading},
                        computed_at=computed_at,
                    )
                )
                anchor_readings += 1
        await session.commit()

    log.info(
        "markets_board_snapshot.done",
        index_id=index_id,
        n_rows=len(payload.get("rows", [])),
        score=payload.get("score"),
        anchor_trends=len(trends),
        anchor_readings=anchor_readings,
        computed_at=computed_at.isoformat(),
    )
    return {
        "index_id": index_id,
        "n_rows": len(payload.get("rows", [])),
        "anchor_trends": len(trends),
        "anchor_readings": anchor_readings,
    }
