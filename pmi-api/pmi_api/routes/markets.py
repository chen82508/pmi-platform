"""Market search — what the site nav's search box hits.

Reads `core_markets` alone: `last_price` / `last_volume_24h` / `url` are
denormalized onto the row (0013, 0015), so a hit needs no join against the
499M-row snapshot table. Matching is `ILIKE '%q%'` served by the trigram GIN
index from 0017 (`idx_core_markets_title_trgm`) — ~60ms over 2M rows.

Public (no API key) like the other read routes the web app calls directly.

Results go through pmi_api.search_cache: a cold query on the saturated data
box takes 17–35 s, and almost all traffic repeats the same terms, so repeats
are served from memory (fresh) or from memory with a background refresh
(stale). ``X-Search-Cache`` reports which path a response took.
"""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.models import CoreMarket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.config import api_settings
from pmi_api.deps import get_session
from pmi_api.schemas import MarketSearchEnvelope, MarketSearchHit, MarketSearchPayload
from pmi_api.search_cache import RedisTier, SearchBusy, SearchCache

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/markets", tags=["markets"])

# Tier 1 lives in this process; tier 2 (Redis) is used when PMI_API_SEARCH_CACHE_REDIS_URL
# is set. See pmi_api.search_cache for the fresh/stale policy.
search_cache = SearchCache(
    fresh_s=api_settings.search_cache_fresh_s,
    stale_s=api_settings.search_cache_stale_s,
    max_entries=api_settings.search_cache_max_entries,
    query_timeout_s=api_settings.search_query_timeout_s,
    refresh_concurrency=api_settings.search_cache_refresh_concurrency,
    l2=(
        RedisTier.from_url(
            api_settings.search_cache_redis_url, ttl_s=api_settings.search_cache_stale_s
        )
        if api_settings.search_cache_redis_url
        else None
    ),
)

# A trigram index can't serve a pattern shorter than one trigram, so anything
# below this would degrade to a sequential scan of every market.
MIN_QUERY_LEN = 3


@router.get("/search", response_model=MarketSearchEnvelope)
async def search_markets(
    response: Response,
    q: str = Query(..., description="Substring of the market title."),
    limit: int = Query(default=20, ge=1, le=100),
    include_resolved: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> MarketSearchEnvelope:
    """Open real-money markets whose title contains `q`, busiest first."""
    query = q.strip()
    if len(query) < MIN_QUERY_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "QUERY_TOO_SHORT",
                    "hint": f"Use at least {MIN_QUERY_LEN} characters.",
                }
            },
        )

    # Escape the LIKE wildcards so a user typing "50%" searches for that text
    # instead of matching everything.
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    stmt = (
        select(
            CoreMarket.id,
            CoreMarket.title,
            CoreMarket.venue,
            CoreMarket.url,
            CoreMarket.last_price,
            CoreMarket.last_volume_24h,
            CoreMarket.closes_at,
        )
        .where(
            CoreMarket.title.ilike(pattern, escape="\\"),
            CoreMarket.is_play_money.is_(False),
        )
        # Volume first so the busiest contract on a topic leads; NULLS LAST
        # keeps never-traded markets from crowding out the real ones.
        .order_by(CoreMarket.last_volume_24h.desc().nullslast(), CoreMarket.id)
        .limit(limit)
    )
    if not include_resolved:
        stmt = stmt.where(*live_market_conditions())

    async def run(s: AsyncSession) -> list[dict]:
        # Plain JSON-safe dicts: the cache may store them in Redis.
        return [
            {
                "market_id": mid,
                "title": title,
                "venue": venue,
                "url": url,
                "last_price": float(price) if price is not None else None,
                "volume_24h": float(vol) if vol is not None else None,
                "closes_at": closes.isoformat() if closes is not None else None,
            }
            for mid, title, venue, url, price, vol, closes in (await s.execute(stmt)).all()
        ]

    # Same normalization pmi-web's search box produces: case and inner spacing
    # don't change the result, so they share one cache entry.
    key = (" ".join(query.lower().split()), limit, include_resolved)
    started = time.monotonic()
    try:
        rows, cache_status = await search_cache.get(key, run, session)
    except SearchBusy as exc:
        log.warning("api.search_busy", query=query, error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "SEARCH_BUSY", "hint": "Retry in a moment."}},
            headers={"Retry-After": "30"},
        ) from exc
    log.info(
        "api.search",
        query=query,
        cache=cache_status,
        seconds=round(time.monotonic() - started, 3),
    )
    response.headers["X-Search-Cache"] = cache_status

    results = [MarketSearchHit(**hit) for hit in rows]
    return MarketSearchEnvelope(
        summary=f'"{query}": {len(results)} market(s).',
        data=MarketSearchPayload(query=query, total=len(results), results=results),
    )
