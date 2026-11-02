"""`workflow` job — executes a durable workflow run (CORR-8.1).

The queue job is the execution vehicle; durability lives in pmi-core's
checkpointing (``core_workflow_steps``). A raised exception here triggers the
queue's backoff retry, and the next execution replays from the last completed
step — that combination IS the Temporal-replacement contract.
"""

from __future__ import annotations

from pmi_core.workflow import execute_run

from pmi_workers.registry import register


@register("workflow", queue="long")
async def run(workflow_run_id: int) -> dict:
    return await execute_run(int(workflow_run_id))
