"""On-demand score path + job/workflow poll endpoints (CORR-4.6 / 8.1 / §3.2).

The worker is NOT running in these tests — assertions cover the enqueue/202
contract and the Postgres-cache freshness logic, not pipeline execution
(that's pmi-workers/tests + the e2e suite).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.models import CoreIndexDefinition, CoreJob, CoreWorkflowRun, TsIndexScore
from sqlalchemy import select


async def test_score_without_freshness_params_is_unchanged(client, seeded_data):
    r = await client.get("/indexes/polymarket-war-index/score")
    assert r.status_code == 200
    assert r.json()["data"]["score"] == seeded_data["expected_score"]


async def test_score_with_max_age_fresh_enough_returns_200(client, session_factory):
    """A score younger than max_age_s serves straight from the Postgres cache.

    Uses a dedicated index def so the shared seeded fixtures (whose 'latest
    score' other test files assert on) stay untouched.
    """
    async with session_factory() as s:
        fresh_def = CoreIndexDefinition(
            index_id="fresh-cache-index",
            version=1,
            title="Fresh Cache Index",
            owner="test",
            definition={"id": "fresh-cache-index", "version": 1},
            yaml_source="id: fresh-cache-index\nversion: 1\n",
            yaml_sha256="1" * 64,
            is_current=True,
            effective_from=datetime.now(UTC) - timedelta(days=1),
        )
        s.add(fresh_def)
        await s.flush()
        s.add(
            TsIndexScore(
                index_definition_id=fresh_def.id,
                as_of=datetime.now(UTC),
                score=61.0,
                component_count=1,
                component_evaluation_ids=[],
                breakdown=None,
            )
        )
        await s.commit()

    r = await client.get("/indexes/fresh-cache-index/score?max_age_s=3600")
    assert r.status_code == 200
    assert r.json()["data"]["score"] == 61.0


async def test_score_stale_returns_202_with_job(client, session_factory):
    r = await client.get("/indexes/empty-index/score?max_age_s=60")
    assert r.status_code == 202
    body = r.json()
    job_id = body["data"]["job_id"]
    assert body["data"]["name"] == "score"
    assert body["data"]["status"] == "queued"

    async with session_factory() as s:
        job = (
            await s.execute(select(CoreJob).where(CoreJob.id == job_id))
        ).scalar_one()
        assert job.args == {"index_id": "empty-index"}
        assert job.dedupe_key == "score:empty-index"
        assert job.priority == 50  # interactive beats cron


async def test_refresh_enqueues_and_dedupes(client):
    r1 = await client.post("/indexes/polymarket-war-index/score/refresh")
    r2 = await client.post("/indexes/polymarket-war-index/score/refresh")
    assert r1.status_code == 202
    assert r2.status_code == 202
    # Second POST while the first is still pending collapses onto the same job.
    assert r1.json()["data"]["job_id"] == r2.json()["data"]["job_id"]


async def test_get_job_status_and_404(client):
    r = await client.post("/indexes/null-score-index/score/refresh")
    job_id = r.json()["data"]["job_id"]

    r = await client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "queued"

    r = await client.get("/jobs/999999")
    assert r.status_code == 404


async def test_backtest_starts_workflow(client, session_factory):
    r = await client.post(
        "/indexes/polymarket-war-index/backtest?days=7&step_hours=24"
    )
    assert r.status_code == 202
    body = r.json()
    run_id = body["data"]["workflow_run_id"]
    assert body["data"]["workflow"] == "backtest"
    assert body["data"]["status"] == "queued"

    async with session_factory() as s:
        run = (
            await s.execute(
                select(CoreWorkflowRun).where(CoreWorkflowRun.id == run_id)
            )
        ).scalar_one()
        assert run.args == {
            "index_id": "polymarket-war-index",
            "days": 7,
            "step_hours": 24,
        }
        assert run.job_id is not None
        job = (
            await s.execute(select(CoreJob).where(CoreJob.id == run.job_id))
        ).scalar_one()
        assert job.name == "workflow"
        assert job.args == {"workflow_run_id": run_id}

    # Poll endpoint sees it; CSV is 409 until the worker finishes it.
    r = await client.get(f"/workflows/{run_id}")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "queued"

    r = await client.get(f"/workflows/{run_id}/csv")
    assert r.status_code == 409


async def test_backtest_pins_version_into_args(client, seeded_data, session_factory):
    """?version=N pins the definition — the version rides into the workflow args
    (SHIP-3.6). Absent → not in args (backward-compatible)."""
    r = await client.post(
        "/indexes/polymarket-war-index/backtest?days=7&version=1"
    )
    assert r.status_code == 202
    run_id = r.json()["data"]["workflow_run_id"]

    async with session_factory() as s:
        run = (
            await s.execute(select(CoreWorkflowRun).where(CoreWorkflowRun.id == run_id))
        ).scalar_one()
        assert run.args == {
            "index_id": "polymarket-war-index",
            "days": 7,
            "step_hours": 24,
            "version": 1,
        }


async def test_backtest_unknown_version_is_404(client, seeded_data):
    """Pinning a version that doesn't exist is a hard 404, not a silent
    fallback to current."""
    r = await client.post(
        "/indexes/polymarket-war-index/backtest?version=999"
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "INDEX_VERSION_NOT_FOUND"
