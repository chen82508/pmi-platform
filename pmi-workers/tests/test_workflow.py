"""pmi_core.workflow durability tests — checkpoint, crash, resume-from-step."""

from __future__ import annotations

import pytest
from pmi_core.models import CoreWorkflowRun, CoreWorkflowStep
from pmi_core.workflow import WORKFLOWS, create_run, execute_run, workflow
from sqlalchemy import select

# ── test workflows (module-level registration; unique names per scenario) ──

EXECUTIONS: dict[str, int] = {}


def _count(key: str):
    async def fn() -> dict:
        EXECUTIONS[key] = EXECUTIONS.get(key, 0) + 1
        return {"key": key, "n": EXECUTIONS[key]}

    return fn


if "t-three-steps" not in WORKFLOWS:

    @workflow("t-three-steps")
    async def t_three_steps(ctx, label: str):
        await ctx.set_steps_total(3)
        points = []
        for i in range(3):
            points.append(await ctx.step(f"s{i}", _count(f"{label}:s{i}")))
        return {"points": points, "label": label}


FLAKY_STATE = {"fail_next": True}

if "t-flaky" not in WORKFLOWS:

    @workflow("t-flaky")
    async def t_flaky(ctx):
        a = await ctx.step("a", _count("flaky:a"))
        if FLAKY_STATE["fail_next"]:
            FLAKY_STATE["fail_next"] = False
            raise RuntimeError("simulated crash between steps")
        b = await ctx.step("b", _count("flaky:b"))
        return {"a": a, "b": b}


# ── tests ──────────────────────────────────────────────────────────────────


async def test_workflow_runs_steps_and_persists_result(session_factory):
    async with session_factory() as s:
        run = await create_run(s, "t-three-steps", {"label": "happy"})
        await s.commit()
        run_id = run.id

    result = await execute_run(run_id)
    assert result["label"] == "happy"
    assert [p["key"] for p in result["points"]] == ["happy:s0", "happy:s1", "happy:s2"]

    async with session_factory() as s:
        row = (
            await s.execute(select(CoreWorkflowRun).where(CoreWorkflowRun.id == run_id))
        ).scalar_one()
        assert row.status == "succeeded"
        assert row.steps_done == 3
        assert row.steps_total == 3
        assert row.result["label"] == "happy"
        steps = (
            await s.execute(
                select(CoreWorkflowStep).where(
                    CoreWorkflowStep.workflow_run_id == run_id
                )
            )
        ).scalars().all()
        assert {st.step_key for st in steps} == {"s0", "s1", "s2"}


async def test_workflow_replay_skips_checkpointed_steps(session_factory):
    """Crash between steps → retry resumes; completed steps don't re-execute."""
    FLAKY_STATE["fail_next"] = True
    EXECUTIONS.pop("flaky:a", None)
    EXECUTIONS.pop("flaky:b", None)

    async with session_factory() as s:
        run = await create_run(s, "t-flaky", {})
        await s.commit()
        run_id = run.id

    with pytest.raises(RuntimeError, match="simulated crash"):
        await execute_run(run_id)

    async with session_factory() as s:
        row = (
            await s.execute(select(CoreWorkflowRun).where(CoreWorkflowRun.id == run_id))
        ).scalar_one()
        assert row.status == "failed"
        assert "simulated crash" in row.error
        # Step 'a' checkpointed before the crash.
        assert row.steps_done == 1

    # Second execution (what the queue retry does) resumes and completes.
    result = await execute_run(run_id)
    assert result["a"]["key"] == "flaky:a"
    assert result["b"]["key"] == "flaky:b"

    # THE durability assertion: step 'a' ran exactly once across both passes
    # (its replay hit the checkpoint), step 'b' ran once.
    assert EXECUTIONS["flaky:a"] == 1
    assert EXECUTIONS["flaky:b"] == 1

    async with session_factory() as s:
        row = (
            await s.execute(select(CoreWorkflowRun).where(CoreWorkflowRun.id == run_id))
        ).scalar_one()
        assert row.status == "succeeded"
        assert row.error is None
        assert row.steps_done == 2


async def test_execute_run_is_idempotent_after_success(session_factory):
    async with session_factory() as s:
        run = await create_run(s, "t-three-steps", {"label": "idem"})
        await s.commit()
        run_id = run.id

    first = await execute_run(run_id)
    again = await execute_run(run_id)  # short-circuits on stored result
    assert again == first
    assert EXECUTIONS["idem:s0"] == 1


async def test_backtest_workflow_is_registered():
    # The worker resolves 'backtest' through the same registry the API
    # creates runs for — guard against the registration import being lost.
    import pmi_core.engine.backtest  # noqa: F401

    assert "backtest" in WORKFLOWS
