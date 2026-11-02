"""Route tests for /indexes/{id}/markets-board (Markets Sub-Index page).

Self-contained fixture (pattern copied from test_routes_senate_board.py's
``attrib_board``): seeds its own index + strike-ladder markets and tears them
down so the global-scan /maga routes never see them. Prices are chosen so the
bucket readings have analytic expectations:

    ladder $59k @ 0.90 → below-spot bucket, reading (2·0.9−1+1)/2·100 = 90
    ladder $60k @ 0.50 → at-the-money,     reading 50
    ladder $61k @ 0.10 → above-spot,        reading 10
    range  bin  @ 0.50 → range bucket,      reading 50

with breakdown.moneyness_weighting.implied_spot = 60,000.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    TsIndexScore,
    TsPriceSnapshot,
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOARD_ID = "markets-board-test"

_FACTORS = [
    {
        "id": "btc_direction",
        "type": "ternary",
        "prompt_ref": "prompts/factors/btc-direction-v1",
        "weight": None,
    },
    {
        "id": "btc_price_relevance",
        "type": "binary",
        "prompt_ref": "prompts/factors/btc-price-relevance-v1",
        "weight": 70,
    },
    {
        "id": "btc_major_move",
        "type": "binary",
        "prompt_ref": "prompts/factors/btc-major-move-v1",
        "weight": 30,
    },
]

# (external_id, title, price)
_MARKETS = [
    ("mb-59k", "Bitcoin price on Jun 24, 2026? - $59,000 or above", 0.90),
    ("mb-60k", "Bitcoin price on Jun 24, 2026? - $60,000 or above", 0.50),
    ("mb-61k", "Bitcoin price on Jun 24, 2026? - $61,000 or above", 0.10),
    ("mb-range", "Bitcoin price range on Jun 24, 2026? - $59,800 to 59,899.99", 0.50),
]


async def _seed_board(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
    *,
    snapshot_offset: timedelta,
    prior_24h: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Seed def + markets + evals + score. ``snapshot_offset`` positions the
    single price snapshot relative to the score's as_of: a positive offset
    (before as_of) gives the spark binner a real window; zero collapses the
    window so spark must come back None. ``prior_24h`` (external_id → price)
    adds a second snapshot 25h back so the same-basket 24h delta has a baseline."""
    as_of: datetime = seeded_data["latest_score_as_of"]
    ir = {
        "id": BOARD_ID,
        "version": 1,
        "title": "Markets Board Test",
        "owner": "test",
        "selectors": [{"type": "keyword", "terms": ["bitcoin"]}],
        "factors": _FACTORS,
        "aggregation": {"formula": "signed_weighted_average_x_100"},
        "venues": ["kalshi"],
    }
    market_ids: list[int] = []
    eval_ids: list[int] = []
    async with session_factory() as session:
        index_def = CoreIndexDefinition(
            index_id=BOARD_ID,
            version=1,
            title="Markets Board Test",
            owner="test",
            definition=ir,
            yaml_source=f"id: {BOARD_ID}\nversion: 1\n",
            yaml_sha256="8" * 64,
            is_current=True,
            effective_from=as_of - timedelta(days=10),
        )
        session.add(index_def)
        await session.flush()

        for external_id, title, price in _MARKETS:
            market = CoreMarket(
                venue="kalshi",
                external_id=external_id,
                slug=external_id,
                title=title,
                description="Markets board test market",
                closes_at=as_of + timedelta(hours=6),
                raw={},
            )
            session.add(market)
            await session.flush()
            market_ids.append(market.id)
            session.add(
                TsPriceSnapshot(
                    market_id=market.id,
                    snapshot_at=as_of - snapshot_offset,
                    last_price=price,
                    volume_24h=1000.0,
                )
            )
            if prior_24h is not None and external_id in prior_24h:
                session.add(
                    TsPriceSnapshot(
                        market_id=market.id,
                        snapshot_at=as_of - timedelta(hours=25),
                        last_price=prior_24h[external_id],
                        volume_24h=1000.0,
                    )
                )
            # Direction +1 (bullish on YES) + both relevancy factors true.
            for factor in _FACTORS:
                evaluation = AuditEvaluation(
                    market_id=market.id,
                    index_definition_id=index_def.id,
                    factor_id=factor["id"],
                    prompt_id=seeded_data["prompt_id"],
                    prompt_sha256="b" * 64,
                    model_id="stub:hash-v0",
                    value_numeric=1.0,
                    confidence=0.9,
                    model_response={"value": 1, "source": "test"},
                )
                session.add(evaluation)
                await session.flush()
                eval_ids.append(evaluation.id)

        session.add(
            TsIndexScore(
                index_definition_id=index_def.id,
                as_of=as_of,
                score=50.0,
                component_count=len(_MARKETS),
                component_evaluation_ids=eval_ids,
                breakdown={"moneyness_weighting": {"implied_spot": 60000.0}},
            )
        )
        await session.commit()
        def_pk = index_def.id
    return {"def_pk": def_pk, "market_ids": market_ids}


async def _teardown_board(
    session_factory: async_sessionmaker[AsyncSession], seeded: dict[str, Any]
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(TsIndexScore).where(
                TsIndexScore.index_definition_id == seeded["def_pk"]
            )
        )
        await session.execute(
            delete(AuditEvaluation).where(
                AuditEvaluation.index_definition_id == seeded["def_pk"]
            )
        )
        await session.execute(
            delete(TsPriceSnapshot).where(
                TsPriceSnapshot.market_id.in_(seeded["market_ids"])
            )
        )
        await session.execute(
            delete(CoreMarket).where(CoreMarket.id.in_(seeded["market_ids"]))
        )
        await session.execute(
            delete(CoreIndexDefinition).where(
                CoreIndexDefinition.id == seeded["def_pk"]
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def board(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    seeded = await _seed_board(
        session_factory, seeded_data, snapshot_offset=timedelta(minutes=5)
    )
    try:
        yield seeded
    finally:
        await _teardown_board(session_factory, seeded)


@pytest_asyncio.fixture
async def board_zero_window(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    seeded = await _seed_board(
        session_factory, seeded_data, snapshot_offset=timedelta(0)
    )
    try:
        yield seeded
    finally:
        await _teardown_board(session_factory, seeded)


@pytest.mark.asyncio
async def test_markets_board_happy_path(
    client: AsyncClient, board: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{BOARD_ID}/markets-board")
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["index_id"] == BOARD_ID
    assert data["score"] == pytest.approx(50.0)
    assert data["regime"] == "Neutral"
    assert data["implied_spot"] == pytest.approx(60000.0)
    assert data["component_count"] == 4
    assert data["exchanges"] == ["kalshi"]
    assert data["holdings"] == len(data["drivers"])

    drivers = {d["key"]: d for d in data["drivers"]}
    # Question-based classify (moneyness sub-buckets were retired): the three
    # "$X or above" titles group into `above`, the "$A to B" title into `range`.
    assert set(drivers) == {"above", "range"}
    # `above` = relevancy-weighted mean of dir·(2p−1) over p∈{.9,.5,.1} (all
    # bullish, equal relevancy) = 0 → 50.
    assert drivers["above"]["value"] == pytest.approx(50.0)
    assert drivers["above"]["n_markets"] == 3
    # `range` = single p=.5 contract → 50.
    assert drivers["range"]["value"] == pytest.approx(50.0)
    assert drivers["range"]["n_markets"] == 1
    # Equal relevancy (1.0 each) → composition shares sum to 100.
    assert sum(d["weight_pct"] for d in data["drivers"]) == pytest.approx(100.0)
    # Snapshot 5 min before as_of → real window → carry-forward fills 12 bins.
    assert all(
        d["spark"] is not None and len(d["spark"]) == 12 for d in data["drivers"]
    )
    # No snapshot ≥14d back → delta unavailable, not fabricated.
    assert all(d["delta_14d"] is None for d in data["drivers"])

    # No snapshot ≥24h back in this fixture → same-basket delta unavailable, and
    # every currently-priced contract counts as "new" vs 24h ago (100% turnover).
    assert data["delta_24h_same_basket"] is None
    assert data["basket_turnover_24h"] == pytest.approx(1.0)

    # series includes at least our seeded point, tagged with its version.
    assert any(p["version"] == 1 and p["score"] == 50.0 for p in data["series"])

    rows = data["rows"]
    assert len(rows) == 4
    for row in rows:
        assert row["venues"] == ["kalshi"]
        assert row["tags"][1] == "Kalshi"
        # No stored url → derived from the Kalshi event ticker.
        assert row["url"].startswith("https://kalshi.com/markets/")
    # `contracts` = size of the row's bucket: 3 for the `above` ladder, 1 for range.
    contracts_by_title = {r["title"]: r["contracts"] for r in rows}
    assert contracts_by_title["Bitcoin price on Jun 24, 2026? - $59,000 or above"] == 3
    assert (
        contracts_by_title["Bitcoin price range on Jun 24, 2026? - $59,800 to 59,899.99"]
        == 1
    )
    probs = {r["title"]: r["prob"] for r in rows}
    assert probs["Bitcoin price on Jun 24, 2026? - $59,000 or above"] == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_markets_board_unknown_index_404(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/definitely-not-an-index/markets-board")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "INDEX_NOT_FOUND"


@pytest.mark.asyncio
async def test_markets_board_no_score_404(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    # conftest seeds `empty-index` with a definition but no scores.
    resp = await client.get("/indexes/empty-index/markets-board")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NO_SCORE_YET"


@pytest.mark.asyncio
async def test_markets_board_zero_window_no_spark(
    client: AsyncClient, board_zero_window: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{BOARD_ID}/markets-board")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # All snapshots land exactly AT as_of → zero-width window → no binning,
    # and no 14d-old prices either. Values still read from latest prices.
    assert all(d["spark"] is None for d in data["drivers"])
    assert all(d["delta_14d"] is None for d in data["drivers"])
    drivers = {d["key"]: d for d in data["drivers"]}
    assert set(drivers) == {"above", "range"}
    # Values still read from latest prices even with no spark window.
    assert drivers["above"]["value"] == pytest.approx(50.0)
    assert drivers["range"]["value"] == pytest.approx(50.0)


@pytest_asyncio.fixture
async def board_with_prior(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    # Current prices .90/.50/.10/.50 (whole-basket reading 50) + a 25h-old
    # snapshot at .80/.40/.05/.50 (reading 43.75) → same-basket Δ24h = +6.25.
    seeded = await _seed_board(
        session_factory,
        seeded_data,
        snapshot_offset=timedelta(minutes=5),
        prior_24h={"mb-59k": 0.80, "mb-60k": 0.40, "mb-61k": 0.05, "mb-range": 0.50},
    )
    try:
        yield seeded
    finally:
        await _teardown_board(session_factory, seeded)


@pytest.mark.asyncio
async def test_markets_board_same_basket_24h_delta(
    client: AsyncClient, board_with_prior: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{BOARD_ID}/markets-board")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # All four contracts priced both now and 24h ago → zero turnover. The delta
    # is the whole-basket reading change — a daily ladder roll would NOT show
    # here because rolled-out legs aren't in both snapshots (CORR-1.14).
    assert data["basket_turnover_24h"] == pytest.approx(0.0)
    assert data["delta_24h_same_basket"] == pytest.approx(6.25)
