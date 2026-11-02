"""reeval-drifted: the drift baseline is the score we're about to replace.

The bug these pin: drift used to be measured against a fixed
`now − PMI_DRIFT_LOOKBACK_HOURS` window, so a market that jumped once stayed
over the threshold for the whole window and its index was re-scored on every
`*/15` beat for the next 24 hours. Two of those full-registry passes then held
the batch lane for hours and the hourly `score-all` never reached the GPU.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    CorePrompt,
    TsIndexScore,
    TsPriceSnapshot,
)
from sqlalchemy import text

from pmi_workers.jobs import reeval_drifted

NOW = datetime.now(UTC)
INDEX_ID = "drift-test-index"


@pytest_asyncio.fixture(autouse=True)
async def _clean(session_factory):
    async with session_factory() as s:
        await s.execute(
            text(
                "TRUNCATE ts_index_scores, audit_evaluations, ts_price_snapshots, "
                "core_markets, core_prompts, core_index_definitions "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()


async def _market(session_factory, external_id: str) -> int:
    """A market whose price jumped 30 points 10h ago, then went flat."""
    async with session_factory() as s:
        m = CoreMarket(venue="polymarket", external_id=external_id, title=external_id)
        s.add(m)
        await s.flush()
        s.add_all(
            [
                TsPriceSnapshot(
                    market_id=m.id, snapshot_at=NOW - timedelta(hours=12), last_price=0.20
                ),
                TsPriceSnapshot(
                    market_id=m.id, snapshot_at=NOW - timedelta(hours=10), last_price=0.50
                ),
                TsPriceSnapshot(
                    market_id=m.id, snapshot_at=NOW - timedelta(minutes=5), last_price=0.50
                ),
            ]
        )
        await s.commit()
        return m.id


async def test_move_outside_the_baseline_is_not_drift(session_factory):
    """Scored an hour ago, flat since → nothing to redo, even though the price
    is 30 points off where it was half a day back."""
    market_id = await _market(session_factory, "flat-since-score")
    async with session_factory() as s:
        drifted = await reeval_drifted._drifted_markets(
            s, {market_id}, NOW - timedelta(hours=1)
        )
    assert drifted == {}


async def test_move_inside_the_baseline_is_drift(session_factory):
    """Same market, but the last score predates the jump → re-score."""
    market_id = await _market(session_factory, "jumped-since-score")
    async with session_factory() as s:
        drifted = await reeval_drifted._drifted_markets(
            s, {market_id}, NOW - timedelta(hours=11)
        )
    assert drifted == {market_id: 30.0}


async def test_market_with_no_baseline_snapshot_is_skipped(session_factory):
    """Nothing at or before the baseline → no way to say it moved."""
    market_id = await _market(session_factory, "younger-than-baseline")
    async with session_factory() as s:
        drifted = await reeval_drifted._drifted_markets(
            s, {market_id}, NOW - timedelta(days=30)
        )
    assert drifted == {}


async def test_components_carry_the_score_timestamp(session_factory):
    """`run()` needs the as_of alongside the market ids — that pairing is what
    anchors each index's drift window to its own last score."""
    market_id = await _market(session_factory, "component")
    as_of = NOW - timedelta(hours=3)
    async with session_factory() as s:
        definition = CoreIndexDefinition(
            index_id=INDEX_ID,
            version=1,
            title="Drift Test",
            owner="test",
            definition={"id": INDEX_ID, "version": 1},
            yaml_source=f"id: {INDEX_ID}\n",
            yaml_sha256="c" * 64,
            is_current=True,
            effective_from=NOW - timedelta(days=1),
        )
        prompt = CorePrompt(
            name="drift-test", version=1, template="x", sha256="d" * 64
        )
        s.add_all([definition, prompt])
        await s.flush()
        evaluation = AuditEvaluation(
            market_id=market_id,
            index_definition_id=definition.id,
            factor_id="f",
            prompt_id=prompt.id,
            prompt_sha256=prompt.sha256,
            model_id="stub",
            model_response={},
            evaluated_at=as_of,
        )
        s.add(evaluation)
        await s.flush()
        s.add(
            TsIndexScore(
                index_definition_id=definition.id,
                as_of=as_of,
                score=50,
                component_count=1,
                component_evaluation_ids=[evaluation.id],
            )
        )
        await s.commit()
        def_id = definition.id

    async with session_factory() as s:
        components = await reeval_drifted._latest_score_components(s, [def_id])
    assert components == {def_id: (as_of, {market_id})}
