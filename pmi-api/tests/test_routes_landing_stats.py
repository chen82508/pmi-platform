"""Route tests for /landing/stats (the homepage hero's headline counts).

The endpoint exists because the hero's three numbers cannot be summed from the
cards it sits above: indexes can read the same contracts, so adding their
counts would claim them twice. These tests pin the deduplication and the
National Economy board's
contribution, which arrives as market ids in its own snapshot because the board
writes no score row and therefore has no lineage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    TsIndexScore,
    TsPageCache,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pmi_api.routes import landing
from pmi_api.routes.landing import _universe_estimate, count_sub_readings

GDP_KEY = "gdp-board:us-gdp-expectation"


@pytest.fixture(autouse=True)
def _fresh_stats_cache():
    """/landing/stats caches per process; each test seeds its own rows, so a
    result cached by one test must not leak into the next."""
    landing._stats_cache.update(at=0.0, data=None)
    yield
    landing._stats_cache.update(at=0.0, data=None)


async def _senate_eval_ids(session: AsyncSession) -> list[int]:
    definition = (
        await session.execute(
            select(CoreIndexDefinition).where(
                CoreIndexDefinition.index_id == "us-senate-2026-republican-seats"
            )
        )
    ).scalar_one()
    score = (
        await session.execute(
            select(TsIndexScore)
            .where(TsIndexScore.index_definition_id == definition.id)
            .order_by(TsIndexScore.as_of.desc())
        )
    ).scalars().first()
    assert score is not None
    return list(score.component_evaluation_ids)


@pytest.mark.asyncio
async def test_counts_only_published_indexes(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """The seeded war/null/empty indexes aren't published, the Senate is.

    `polymarket-war-index` is a fixture id, not one of the site's indexes, so
    it must not inflate the hero — the catalogue and the published set are
    different things.
    """
    data = (await client.get("/landing/stats")).json()["data"]

    assert data["indexes"] == 1
    # The Senate fixture's lineage covers its three component markets, all on
    # Polymarket; the war fixture's market is excluded with its definition.
    assert data["contracts"] == 3
    assert data["venues"] == ["polymarket"]
    assert data["as_of"] is not None


@pytest.mark.asyncio
async def test_universe_is_the_corpus_not_the_lineage(
    client: AsyncClient,
    seeded_data: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`universe` counts every ingested market; `contracts` counts the readings'.

    /methodology quotes this as the pool step 1 selects FROM, so it has to
    include the markets no published index reads — here, the war fixture's,
    which `contracts` deliberately excludes.
    """
    async with session_factory() as session:
        # reltuples is -1 until the table has been analyzed at least once, and
        # the route reports that as "unknown" rather than a negative count.
        await session.execute(text("ANALYZE core_markets"))
        await session.commit()
        total = (
            await session.execute(select(func.count()).select_from(CoreMarket))
        ).scalar_one()

    data = (await client.get("/landing/stats")).json()["data"]

    assert data["universe"] == total
    assert data["universe"] > data["contracts"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (2_203_918, 2_203_918),
        # What `reltuples` reports for a table autovacuum has never analyzed.
        (-1, None),
        (0, None),
        (None, None),
    ],
)
def test_universe_estimate_rejects_unknown(raw: float | None, expected: int | None) -> None:
    """-1 is Postgres for "never analyzed", not a count — it must not reach the page."""
    assert _universe_estimate(raw) == expected


@pytest_asyncio.fixture
async def house_sharing_senate_markets(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
):
    """A second published index reading the SAME contracts as the Senate one.

    Stands in for the real overlap the endpoint exists for: indexes reading the
    same 2026 race markets.
    """
    async with session_factory() as session:
        senate_ids = await _senate_eval_ids(session)
        rows = (
            await session.execute(
                select(AuditEvaluation).where(AuditEvaluation.id.in_(senate_ids))
            )
        ).scalars().all()

        definition = CoreIndexDefinition(
            index_id="us-house-2026-republican-seats",
            version=1,
            title="US House 2026",
            owner="test",
            definition={"id": "us-house-2026-republican-seats", "version": 1},
            yaml_source="id: us-house-2026-republican-seats\nversion: 1\n",
            yaml_sha256="c" * 64,
            is_current=True,
            effective_from=datetime.now(UTC) - timedelta(days=10),
        )
        session.add(definition)
        await session.flush()

        clone_ids: list[int] = []
        for row in rows:
            clone = AuditEvaluation(
                market_id=row.market_id,
                index_definition_id=definition.id,
                factor_id="house_seat",
                prompt_id=row.prompt_id,
                prompt_sha256=row.prompt_sha256,
                model_id=row.model_id,
                value_numeric=1.0,
                confidence=0.9,
                model_response={"value": 1, "source": "test"},
            )
            session.add(clone)
            await session.flush()
            clone_ids.append(clone.id)

        session.add(
            TsIndexScore(
                index_definition_id=definition.id,
                as_of=datetime.now(UTC),
                score=51.0,
                component_count=len(clone_ids),
                component_evaluation_ids=clone_ids,
                breakdown={},
            )
        )
        await session.commit()
        def_id = definition.id

    yield def_id

    async with session_factory() as session:
        await session.execute(
            delete(TsIndexScore).where(TsIndexScore.index_definition_id == def_id)
        )
        await session.execute(
            delete(AuditEvaluation).where(AuditEvaluation.index_definition_id == def_id)
        )
        await session.execute(
            delete(CoreIndexDefinition).where(CoreIndexDefinition.id == def_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_shared_contracts_are_counted_once(
    client: AsyncClient, house_sharing_senate_markets: int
) -> None:
    """Two indexes over the same three contracts is still three contracts."""
    data = (await client.get("/landing/stats")).json()["data"]

    assert data["indexes"] == 2
    assert data["contracts"] == 3


@pytest_asyncio.fixture
async def gdp_board_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
):
    """A board snapshot naming one contract the Senate already reads and one new.

    The economy board is not an engine index — no score row, no lineage — so
    the ids in its own payload are the only way its contracts can be counted,
    and the only way they can be deduplicated against everything else.
    """
    async with session_factory() as session:
        shared = (
            await session.execute(
                select(CoreMarket.id).where(CoreMarket.external_id == "seed-senate-0")
            )
        ).scalar_one()
        fresh = CoreMarket(
            venue="kalshi",
            external_id="gdp-stats-1",
            slug="us-gdp-2026",
            title="US GDP growth above 2% in 2026?",
            last_price=0.44,
        )
        session.add(fresh)
        await session.flush()
        session.add(
            TsPageCache(
                cache_key=GDP_KEY,
                payload={
                    "score": 49.6,
                    "as_of": datetime.now(UTC).isoformat(),
                    "market_ids": [shared, fresh.id],
                },
                computed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        fresh_id = fresh.id

    yield fresh_id

    async with session_factory() as session:
        await session.execute(delete(TsPageCache).where(TsPageCache.cache_key == GDP_KEY))
        await session.execute(delete(CoreMarket).where(CoreMarket.id == fresh_id))
        await session.commit()


@pytest.mark.asyncio
async def test_economy_board_adds_its_own_contracts_once(
    client: AsyncClient, gdp_board_snapshot: int
) -> None:
    data = (await client.get("/landing/stats")).json()["data"]

    # +1 index (the board has a score) and +1 contract — the shared one is not
    # counted twice, and its venue now joins the list.
    assert data["indexes"] == 2
    assert data["contracts"] == 4
    assert data["venues"] == ["kalshi", "polymarket"]


def test_sub_readings_count_only_scored_entries() -> None:
    """Each drill-down counts once, and only when it carries a score."""
    payloads = {
        "war": {
            "countries": {"ua": {"heat": 40.0}, "ru": {"heat": 35.0}, "fj": {"heat": None}},
            "regions": {"europe": {"heat": 30.0}},
        },
        "senate": {"races": [{"prob_r": 60.0}, {"prob_r": 40.0}, {"prob_r": None}]},
        "house": None,
        # Two strikes on one question are one reading; an unpriced question is none.
        "btc": {"rows": [
            {"anchor": "btc-eoy", "prob": 55.0},
            {"anchor": "btc-eoy", "prob": 30.0},
            {"anchor": "btc-dec", "prob": None},
        ]},
        "gdp": {"anchors": [{"sentiment": 52.0}, {"sentiment": None}]},
    }
    # war 2 + 1, senate 2, btc 1, gdp 1
    assert count_sub_readings(payloads) == 7
    assert count_sub_readings({}) == 0


@pytest.mark.asyncio
async def test_readings_include_the_cards(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """With no drill-down snapshots, readings is just the live card count."""
    data = (await client.get("/landing/stats")).json()["data"]
    assert data["readings"] >= data["indexes"]
