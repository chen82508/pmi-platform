"""Wire MLflow links into core_prompts / audit_evaluations / core_index_definitions /
audit_pipeline_runs.

Pure additive: every new column is nullable. Existing rows untouched.
Pipeline still works if MLflow is down — these columns just stay NULL.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "core_prompts",
        sa.Column("mlflow_prompt_uri", sa.String(256), nullable=True),
    )

    op.add_column(
        "core_index_definitions",
        sa.Column("mlflow_experiment_id", sa.String(64), nullable=True),
    )

    op.add_column(
        "audit_evaluations",
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "audit_evaluations",
        sa.Column("latency_ms", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_audit_evaluations__mlflow_run_id",
        "audit_evaluations",
        ["mlflow_run_id"],
    )

    op.add_column(
        "audit_pipeline_runs",
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_audit_pipeline_runs__mlflow_run_id",
        "audit_pipeline_runs",
        ["mlflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_pipeline_runs__mlflow_run_id", table_name="audit_pipeline_runs")
    op.drop_column("audit_pipeline_runs", "mlflow_run_id")
    op.drop_index("ix_audit_evaluations__mlflow_run_id", table_name="audit_evaluations")
    op.drop_column("audit_evaluations", "latency_ms")
    op.drop_column("audit_evaluations", "mlflow_run_id")
    op.drop_column("core_index_definitions", "mlflow_experiment_id")
    op.drop_column("core_prompts", "mlflow_prompt_uri")
