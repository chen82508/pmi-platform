"""Route test for /explain cross-venue peer grouping (peer_group_id).

Self-contained fixture (same pattern as test_routes_senate_board.attrib_board):
three component markets — a Polymarket/Kalshi pair with IDENTICAL embeddings
(cosine 1.0 → one peer group) and a third market with an orthogonal embedding
(singleton). A fourth same-venue pair with identical embeddings must NOT
group (same-venue edges are excluded so Polymarket's intentional date-ladder
variants stay separate cards). Torn down afterwards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
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
    VecMarketEmbedding,
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PEERS_ID = "explain-peers-test"

# 8-dim toy vectors under a dedicated model name; group_peers only compares
# embeddings sharing a model, so these never meet production rows.
DIM = 8
V_SAME = [1.0] + [0.0] * (DIM - 1)
V_OTHER = [0.0, 1.0] + [0.0] * (DIM - 2)
MODEL = "test-embed"


@pytest_asyncio.fixture
async def peers_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    as_of = seeded_data["latest_score_as_of"]
    markets = [
        # (external_id, venue, title, embedding)
        ("peers-poly", "polymarket", "Russia x Ukraine ceasefire in 2026?", V_SAME),
        ("peers-kalshi", "kalshi", "Russia-Ukraine ceasefire before 2027?", V_SAME),
        ("peers-other", "polymarket", "Will China invade Taiwan in 2026?", V_OTHER),
        # Same-venue near-duplicates (date-ladder variants) — identical
        # embeddings but must remain separate groups.
        ("peers-ladder-a", "polymarket", "Ceasefire by October 31?", V_OTHER),
        ("peers-ladder-b", "polymarket", "Ceasefire by December 31?", V_OTHER),
    ]
    ir = {
        "id": PEERS_ID,
        "version": 1,
        "title": "Explain Peers Test",
        "owner": "test",
        "selectors": [{"type": "keyword", "terms": ["ceasefire"]}],
        "factors": [
            {
                "id": "directly_about_war",
                "type": "binary",
                "prompt_ref": "prompts/factors/directly_about_war-v1",
                "weight": 1.0,
            },
        ],
    }

    market_ids: list[int] = []
    eval_ids: list[int] = []
    async with session_factory() as session:
        index_def = CoreIndexDefinition(
            index_id=PEERS_ID,
            version=1,
            title="Explain Peers Test",
            owner="test",
            definition=ir,
            yaml_source=f"id: {PEERS_ID}\nversion: 1\n",
            yaml_sha256="8" * 64,
            is_current=True,
            effective_from=as_of - timedelta(days=10),
        )
        session.add(index_def)
        await session.flush()

        for external_id, venue, title, embedding in markets:
            market = CoreMarket(
                venue=venue,
                external_id=external_id,
                slug=external_id,
                title=title,
                description="Peer grouping test market",
            )
            session.add(market)
            await session.flush()
            market_ids.append(market.id)
            session.add_all([
                TsPriceSnapshot(
                    market_id=market.id,
                    snapshot_at=as_of - timedelta(minutes=5),
                    last_price=0.5,
                    volume_24h=1000.0,
                ),
                VecMarketEmbedding(
                    market_id=market.id,
                    model=MODEL,
                    dim=DIM,
                    embedding=embedding,
                    text_sha256="0" * 64,
                ),
            ])
            evaluation = AuditEvaluation(
                market_id=market.id,
                index_definition_id=index_def.id,
                factor_id="directly_about_war",
                prompt_id=seeded_data["prompt_id"],
                prompt_sha256="a" * 64,
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
                component_count=len(markets),
                component_evaluation_ids=eval_ids,
                breakdown={"raw": 0.0},
            )
        )
        await session.commit()
        def_pk = index_def.id

    try:
        yield {"market_ids": market_ids}
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(TsIndexScore).where(TsIndexScore.index_definition_id == def_pk)
            )
            await session.execute(
                delete(AuditEvaluation).where(AuditEvaluation.id.in_(eval_ids))
            )
            for table in (VecMarketEmbedding, TsPriceSnapshot):
                await session.execute(
                    delete(table).where(table.market_id.in_(market_ids))
                )
            await session.execute(delete(CoreMarket).where(CoreMarket.id.in_(market_ids)))
            await session.execute(
                delete(CoreIndexDefinition).where(CoreIndexDefinition.id == def_pk)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_explain_groups_cross_venue_peers(
    client: AsyncClient, peers_fixture: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{PEERS_ID}/explain")
    assert resp.status_code == 200
    components = resp.json()["components"]
    assert len(components) == 5

    by_slug_title = {c["title"]: c for c in components}
    poly = by_slug_title["Russia x Ukraine ceasefire in 2026?"]
    kalshi = by_slug_title["Russia-Ukraine ceasefire before 2027?"]
    other = by_slug_title["Will China invade Taiwan in 2026?"]
    ladder_a = by_slug_title["Ceasefire by October 31?"]
    ladder_b = by_slug_title["Ceasefire by December 31?"]

    # Cross-venue identical embeddings → one group, rooted at the smaller id.
    assert poly["peer_group_id"] == kalshi["peer_group_id"]
    assert poly["peer_group_id"] == min(poly["market_id"], kalshi["market_id"])

    # Orthogonal embedding → singleton (its own market_id).
    assert other["peer_group_id"] == other["market_id"]
    assert other["peer_group_id"] != poly["peer_group_id"]

    # Same-venue near-duplicates never edge together (date-ladder variants
    # stay separate holdings) even with identical embeddings.
    assert ladder_a["peer_group_id"] == ladder_a["market_id"]
    assert ladder_b["peer_group_id"] == ladder_b["market_id"]
    assert ladder_a["peer_group_id"] != ladder_b["peer_group_id"]
