"""Route tests for /indexes/{id}/senate-board (SHIP-2.5 + CORR-1.3).

Two layers:

* Distribution + basic attribution against the shared ``seeded_data`` senate
  fixture (two 50/50 seats, holdover 49/49 → analytic Poisson-binomial).
* Step-2 attribution (matchup / contracts / exchanges / delta_14d) against a
  self-contained index seeded and torn down by the ``attrib_board`` fixture —
  kept OUT of conftest because the /maga endpoints scan ALL race markets
  globally, so extra race markets / old snapshots in the session fixture
  would silently shift the maga route assertions.
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
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SENATE_ID = "us-senate-2026-republican-seats"
ATTRIB_ID = "senate-board-attrib-test"


# ──────────────────────────────────────────────────────────────────────────
# Shared-fixture board: deterministic distribution + title-parse attribution
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_board_distribution_is_deterministic(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{SENATE_ID}/senate-board")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Two contested seats at 0.50 + holdover 49R/49D (see conftest).
    assert data["n_contested"] == 2
    assert data["expected_r_seats"] == pytest.approx(50.0)
    assert data["p_r_majority"] == pytest.approx(25.0)
    assert data["p_d_majority"] == pytest.approx(25.0)
    assert data["counts"]["tossup"] == 2
    assert data["holdover_r"] == 49
    assert data["holdover_d"] == 49


@pytest.mark.asyncio
async def test_board_basic_race_attribution(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{SENATE_ID}/senate-board")
    assert resp.status_code == 200
    data = resp.json()["data"]
    races = {r["state"]: r for r in data["races"]}
    assert set(races) == {"NC", "TX"}
    for race in races.values():
        assert race["prob_r"] == pytest.approx(50.0)
        assert race["band"] == "tossup"
        # Single-sided seats with no gamma payload: contracts/exchanges from
        # the one underlying market; matchup needs candidate names → null.
        assert race["contracts"] == 1
        assert race["exchanges"] == ["polymarket"]
        assert race["matchup"] is None
        # No snapshot ≥14d back in the shared fixture → null delta.
        assert race["delta_14d"] is None
        # Incumbency is filled at serve time from the static roster: NC and TX
        # are both Republican-held Class II seats in the 2026 cycle.
        assert race["incumbent_party"] == "R"
    assert data["prob_by_state"] == {
        "NC": pytest.approx(50.0),
        "TX": pytest.approx(50.0),
    }


@pytest.mark.asyncio
async def test_board_404_when_no_score(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/empty-index/senate-board")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NO_SCORE_YET"


@pytest.mark.asyncio
async def test_board_404_for_non_seat_index(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """A scored index that doesn't project seats has no board. Before this
    guard it answered 200 by collapsing unrelated components into fake seats
    against the _SEAT_DEFAULTS geometry — minutes of scanning for nonsense."""
    resp = await client.get("/indexes/polymarket-war-index/senate-board")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_A_SEAT_BOARD"


# ──────────────────────────────────────────────────────────────────────────
# Step-2 attribution: matchup / contracts / exchanges / delta_14d
# ──────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def attrib_board(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Self-contained senate-board index exercising step-2 attribution.

    Ohio: R + D markets with candidate groupItemTitles (→ matchup, contracts=2)
    and a 15-day-old snapshot on the R market at 0.40 (→ delta_14d = +10.0).
    Georgia: only the D market is a score component; the R market exists in
    core_markets but has no evaluation — exercising the complement-enrichment
    scan that fills the missing matchup side from outside the score lineage
    (on live data the relevancy factor zeroes D-side markets out of the
    components, so this is the common case). Torn down afterwards so the
    global-scan /maga routes never see these extra race markets.
    """
    as_of = seeded_data["latest_score_as_of"]
    markets = [
        # (external_id, title, price, raw, prior_price_15d, is_component)
        (
            "attrib-oh-r",
            "Will the Republicans win the Ohio Senate race in 2026?",
            0.50,
            {"groupItemTitle": "Jon Husted (R)"},
            0.40,
            True,
        ),
        (
            "attrib-oh-d",
            "Will the Democrats win the Ohio Senate race in 2026?",
            0.50,
            {"groupItemTitle": "Sherrod Brown (D)"},
            None,
            True,
        ),
        (
            "attrib-ga-d",
            "Will the Democrats win the Georgia Senate race in 2026?",
            0.65,
            {"groupItemTitle": "Jon Ossoff (D)"},
            None,
            True,
        ),
        (
            "attrib-ga-r",
            "Will the Republicans win the Georgia Senate race in 2026?",
            0.35,
            {"groupItemTitle": "Derek Dooley (R)"},
            None,
            False,  # not a component — found only by the complement scan
        ),
    ]
    ir = {
        "id": ATTRIB_ID,
        "version": 1,
        "title": "Senate Board Attribution Test",
        "owner": "test",
        "selectors": [{"type": "keyword", "terms": ["senate"]}],
        "factors": [
            {
                "id": "republican_on_yes",
                "type": "binary",
                "prompt_ref": "prompts/factors/republican-on-yes-v1",
                "weight": 1.0,
            },
        ],
        "aggregation": {
            "formula": "seat_projection_sum",
            "seat_projection": {
                "total_seats": 100,
                "majority_threshold": 51,
                "holdover_r": 49,
                "holdover_d": 49,
            },
        },
    }

    market_ids: list[int] = []
    eval_ids: list[int] = []
    async with session_factory() as session:
        index_def = CoreIndexDefinition(
            index_id=ATTRIB_ID,
            version=1,
            title="Senate Board Attribution Test",
            owner="test",
            definition=ir,
            yaml_source=f"id: {ATTRIB_ID}\nversion: 1\n",
            yaml_sha256="9" * 64,
            is_current=True,
            effective_from=as_of - timedelta(days=10),
        )
        session.add(index_def)
        await session.flush()

        for external_id, title, price, raw, prior_price, is_component in markets:
            market = CoreMarket(
                venue="polymarket",
                external_id=external_id,
                slug=external_id,
                title=title,
                description="Attribution test market",
                raw=raw,
            )
            session.add(market)
            await session.flush()
            market_ids.append(market.id)
            session.add(
                TsPriceSnapshot(
                    market_id=market.id,
                    snapshot_at=as_of - timedelta(minutes=5),
                    last_price=price,
                    volume_24h=1000.0,
                )
            )
            if prior_price is not None:
                session.add(
                    TsPriceSnapshot(
                        market_id=market.id,
                        snapshot_at=as_of - timedelta(days=15),
                        last_price=prior_price,
                        volume_24h=500.0,
                    )
                )
            if is_component:
                evaluation = AuditEvaluation(
                    market_id=market.id,
                    index_definition_id=index_def.id,
                    factor_id="republican_on_yes",
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
                component_count=2,
                component_evaluation_ids=eval_ids,
                breakdown={"raw": 0.0},
            )
        )
        await session.commit()
        def_pk = index_def.id

    try:
        yield {"def_pk": def_pk, "market_ids": market_ids}
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(TsIndexScore).where(TsIndexScore.index_definition_id == def_pk)
            )
            await session.execute(
                delete(AuditEvaluation).where(AuditEvaluation.id.in_(eval_ids))
            )
            await session.execute(
                delete(TsPriceSnapshot).where(TsPriceSnapshot.market_id.in_(market_ids))
            )
            await session.execute(delete(CoreMarket).where(CoreMarket.id.in_(market_ids)))
            await session.execute(
                delete(CoreIndexDefinition).where(CoreIndexDefinition.id == def_pk)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_board_step2_attribution(
    client: AsyncClient, attrib_board: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{ATTRIB_ID}/senate-board")
    assert resp.status_code == 200
    data = resp.json()["data"]
    races = {r["state"]: r for r in data["races"]}
    assert set(races) == {"OH", "GA"}

    ohio = races["OH"]
    # Matchup from the two candidate groupItemTitles; the R market is the
    # representative, so prob_r reads its price directly.
    assert ohio["matchup"] == "Jon Husted (R) vs Sherrod Brown (D)"
    assert ohio["contracts"] == 2  # R + D markets collapsed to one seat
    assert ohio["exchanges"] == ["polymarket"]
    # 0.50 now vs 0.40 fifteen days ago → +10.0 pct-pts.
    assert ohio["delta_14d"] == pytest.approx(10.0)
    assert ohio["prob_r"] == pytest.approx(50.0)

    georgia = races["GA"]
    # Only the D market is a score component (P(R) flips its price), but the
    # complement scan pulls the non-component R market's candidate name in.
    assert georgia["prob_r"] == pytest.approx(35.0)
    assert georgia["matchup"] == "Derek Dooley (R) vs Jon Ossoff (D)"
    assert georgia["contracts"] == 2
    assert georgia["delta_14d"] is None
