"""core_jobs + core_workflow_runs/steps — Postgres queue (CORR-4.6) and
durable workflows (CORR-8.1), both Redis/Temporal-free by design decision
(2026-06-11: single-EC2 deployment keeps Postgres as the only stateful infra).

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("queue", sa.String(32), nullable=False, server_default="default"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dedupe_key", sa.String(256)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
    )
    op.create_index(
        "ix_core_jobs__claim", "core_jobs", ["status", "queue", "run_at", "priority"]
    )
    # Dedupe is enforced only while the job is pending — finished jobs never
    # block a re-enqueue of the same key.
    op.create_index(
        "uq_core_jobs__dedupe_pending",
        "core_jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "core_workflow_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow", sa.String(128), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("core_jobs.id", name="fk_core_workflow_runs__job_id__core_jobs"),
        ),
        sa.Column("steps_total", sa.Integer()),
        sa.Column("steps_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "core_workflow_steps",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "core_workflow_runs.id",
                name="fk_core_workflow_steps__workflow_run_id__core_workflow_runs",
            ),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="succeeded"),
        sa.Column("result", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "workflow_run_id", "step_key", name="uq_core_workflow_steps__run_step"
        ),
    )
    op.create_index(
        "ix_core_workflow_steps__run", "core_workflow_steps", ["workflow_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_core_workflow_steps__run", table_name="core_workflow_steps")
    op.drop_table("core_workflow_steps")
    op.drop_table("core_workflow_runs")
    op.drop_index("uq_core_jobs__dedupe_pending", table_name="core_jobs")
    op.drop_index("ix_core_jobs__claim", table_name="core_jobs")
    op.drop_table("core_jobs")
