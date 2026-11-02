"""Route tests for /markets/search — what the site nav's search box hits.

Self-contained fixture (same pattern as test_routes_senate_board.attrib_board):
the maga routes scan all race markets globally, so extra markets in the session
fixture would shift their assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pmi_core.models import CoreMarket
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pmi_api.routes import markets

# Deliberately not a race/war phrasing, so the other suites' global scans ignore
# these rows.
PREFIX = "zzsearchfixture"


@pytest.fixture(autouse=True)
def _fresh_search_cache():
    """The search cache is per process; tests insert and delete their own
    rows, so a result cached by one test must not leak into the next."""
    markets.search_cache.clear()
    yield
    markets.search_cache.clear()


@pytest_asyncio.fixture
async def searchable(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    now = datetime.now(UTC)
    rows = [
        CoreMarket(
            venue="polymarket",
            external_id=f"{PREFIX}-busy",
            title=f"{PREFIX} busy contract",
            last_price=0.62,
            last_volume_24h=90_000.0,
            closes_at=now + timedelta(days=30),
            url="https://polymarket.com/busy",
        ),
        CoreMarket(
            venue="kalshi",
            external_id=f"{PREFIX}-quiet",
            title=f"{PREFIX} quiet contract",
            last_price=0.11,
            last_volume_24h=12.0,
        ),
        CoreMarket(
            venue="polymarket",
            external_id=f"{PREFIX}-settled",
            title=f"{PREFIX} settled contract",
            last_price=1.0,
            last_volume_24h=500_000.0,
            resolved_at=now - timedelta(days=1),
            resolution="YES",
        ),
        CoreMarket(
            venue="manifold",  # is_play_money is generated from venue
            external_id=f"{PREFIX}-play",
            title=f"{PREFIX} play-money contract",
            last_price=0.5,
            last_volume_24h=400_000.0,
        ),
        CoreMarket(
            venue="polymarket",
            external_id=f"{PREFIX}-pct",
            title=f"{PREFIX} 50% chance literal",
            last_price=0.5,
            last_volume_24h=1_000.0,
        ),
    ]
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()

    yield {"prefix": PREFIX}

    async with session_factory() as session:
        await session.execute(
            delete(CoreMarket).where(CoreMarket.external_id.like(f"{PREFIX}-%"))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_search_ranks_by_volume_and_hides_settled_and_play_money(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    resp = await client.get(f"/markets/search?q={PREFIX}")
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    titles = [r["title"] for r in results]

    assert titles[0] == f"{PREFIX} busy contract"  # busiest open market leads
    assert f"{PREFIX} quiet contract" in titles
    # A settled market and a play-money one both out-volume everything here, so
    # their absence is the filter working rather than the ordering.
    assert f"{PREFIX} settled contract" not in titles
    assert f"{PREFIX} play-money contract" not in titles


@pytest.mark.asyncio
async def test_search_returns_the_fields_the_holdings_list_needs(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    resp = await client.get(f"/markets/search?q={PREFIX} busy")
    assert resp.status_code == 200
    (hit,) = resp.json()["data"]["results"]
    assert hit["venue"] == "polymarket"
    assert hit["url"] == "https://polymarket.com/busy"
    assert hit["last_price"] == pytest.approx(0.62)
    assert hit["volume_24h"] == pytest.approx(90_000.0)
    assert hit["closes_at"] is not None


@pytest.mark.asyncio
async def test_include_resolved_opts_settled_markets_back_in(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    resp = await client.get(f"/markets/search?q={PREFIX}&include_resolved=true")
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()["data"]["results"]]
    assert f"{PREFIX} settled contract" in titles


@pytest.mark.asyncio
async def test_percent_is_searched_literally_not_as_a_wildcard(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    """Unescaped, '%' would make the LIKE match every market in the table."""
    resp = await client.get("/markets/search?q=50%25 chance")
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()["data"]["results"]]
    assert titles == [f"{PREFIX} 50% chance literal"]


@pytest.mark.asyncio
async def test_short_query_is_rejected_rather_than_scanned(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    """Below one trigram the GIN index can't serve the pattern."""
    resp = await client.get("/markets/search?q=ab")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "QUERY_TOO_SHORT"


@pytest.mark.asyncio
async def test_limit_caps_the_result_set(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    resp = await client.get(f"/markets/search?q={PREFIX}&limit=1")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["results"]) == 1


@pytest.mark.asyncio
async def test_repeat_search_is_served_from_the_cache(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    first = await client.get(f"/markets/search?q={PREFIX}")
    second = await client.get(f"/markets/search?q={PREFIX}")
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Search-Cache"] == "miss"
    assert second.headers["X-Search-Cache"] == "hit"
    assert second.json() == first.json()


@pytest.mark.asyncio
async def test_case_and_spacing_variants_share_one_cache_entry(
    client: AsyncClient, searchable: dict[str, Any]
) -> None:
    await client.get(f"/markets/search?q={PREFIX} quiet")
    variant = await client.get(f"/markets/search?q={PREFIX.upper()}%20%20QUIET")
    assert variant.headers["X-Search-Cache"] == "hit"
