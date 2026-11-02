"""Route tests for /landing/venue-spread (the homepage's cross-venue example).

The endpoint's whole reason for existing is the split between what is cached
and what is live: `venue-spread-snapshot` picks WHICH two contracts to show
(an expensive cosine join), and the route reads their prices at request time.
So the tests below pin that split — a cached price must never reach the page —
plus the refusal paths, because the landing renders nothing at all rather than
a stale or stock example.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pmi_core.models import CoreMarket, TsPageCache
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CACHE_KEY = "landing:venue-spread"


def _payload(a_id: int, b_id: int) -> dict:
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "n_cross_venue_markets": 54,
        "pair": {
            "index_id": "maga-governor-2026",
            "tag": "MAGA Index",
            "href": "/micah",
            "similarity": 0.934,
            "markets": [
                {
                    "market_id": a_id,
                    "venue": "polymarket",
                    "title": "Will the Democrats win the Wisconsin governor race in 2026?",
                    # Deliberately stale: the route must not serve this.
                    "url": "https://polymarket.test/stale",
                },
                {
                    "market_id": b_id,
                    "venue": "kalshi",
                    "title": "Will the Democratic party win the governorship in Wisconsin",
                    "url": "https://kalshi.test/wi",
                },
            ],
        },
    }


@pytest_asyncio.fixture
async def spread_pair(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict]:
    """Two real-money markets on one question + the snapshot that picked them."""
    async with session_factory() as session:
        a = CoreMarket(
            venue="polymarket",
            external_id="vs-poly-wi",
            slug="wi-gov-d",
            title="Will the Democrats win the Wisconsin governor race in 2026?",
            url="https://polymarket.test/wi",
            last_price=0.57,
            last_volume_24h=18351.0,
        )
        b = CoreMarket(
            venue="kalshi",
            external_id="vs-kalshi-wi",
            slug="wi-gov-d-kalshi",
            title="Will the Democratic party win the governorship in Wisconsin",
            url="https://kalshi.test/wi",
            last_price=0.80,
            last_volume_24h=25068.0,
        )
        session.add_all([a, b])
        await session.flush()
        session.add(
            TsPageCache(
                cache_key=CACHE_KEY,
                payload=_payload(a.id, b.id),
                computed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        ids = {"a": a.id, "b": b.id}

    yield ids

    async with session_factory() as session:
        await session.execute(delete(TsPageCache).where(TsPageCache.cache_key == CACHE_KEY))
        await session.execute(delete(CoreMarket).where(CoreMarket.id.in_([ids["a"], ids["b"]])))
        await session.commit()


@pytest.mark.asyncio
async def test_returns_live_prices_deepest_book_first(
    client: AsyncClient, spread_pair: dict
) -> None:
    pair = (await client.get("/landing/venue-spread")).json()["data"]["pair"]

    assert [q["venue"] for q in pair["quotes"]] == ["kalshi", "polymarket"]
    assert [q["prob"] for q in pair["quotes"]] == [80.0, 57.0]
    assert pair["spread_pts"] == 23.0
    assert pair["index"]["tag"] == "MAGA Index"
    assert pair["n_cross_venue_markets"] == 54
    # The sub-index score is deliberately absent — see the route's comment.
    assert "score" not in pair["index"]
    # The url comes from core_markets, not the snapshot's copy of it.
    assert pair["quotes"][1]["url"] == "https://polymarket.test/wi"


@pytest.mark.asyncio
async def test_price_move_shows_immediately(
    client: AsyncClient,
    spread_pair: dict,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A price change is reflected without re-running the snapshot job."""
    async with session_factory() as session:
        market = await session.get(CoreMarket, spread_pair["a"])
        assert market is not None
        market.last_price = 0.62
        await session.commit()

    pair = (await client.get("/landing/venue-spread")).json()["data"]["pair"]
    assert [q["prob"] for q in pair["quotes"]] == [80.0, 62.0]
    assert pair["spread_pts"] == 18.0


@pytest.mark.asyncio
async def test_resolved_side_drops_the_pair(
    client: AsyncClient,
    spread_pair: dict,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Once either contract settles the example is history, not a reading."""
    async with session_factory() as session:
        market = await session.get(CoreMarket, spread_pair["b"])
        assert market is not None
        market.resolved_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

    assert (await client.get("/landing/venue-spread")).json()["data"]["pair"] is None


@pytest.mark.asyncio
async def test_no_snapshot_yields_no_pair(client: AsyncClient) -> None:
    """Nothing computed yet → null, so the landing omits the block entirely."""
    assert (await client.get("/landing/venue-spread")).json()["data"]["pair"] is None
