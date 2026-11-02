"""core_factor_models — (prompt + LLM + temp + tools) bundle for the evaluator
to dereference at runtime. Mirrors MLflow Model Registry.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_factor_models",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("factor_id", sa.String(64), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column(
            "prompt_id",
            sa.BigInteger,
            sa.ForeignKey("core_prompts.id"),
            nullable=False,
        ),
        sa.Column("llm_model_id", sa.String(64), nullable=False),
        sa.Column("temperature", sa.Numeric(4, 3)),
        sa.Column("tools_config", sa.JSON()),
        sa.Column("mlflow_registered_model_name", sa.String(128)),
        sa.Column("mlflow_model_version", sa.String(32)),
        sa.Column(
            "stage",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'staging'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("description", sa.String(512)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(128)),
        sa.UniqueConstraint(
            "factor_id", "version", name="uq_core_factor_models__factor_version"
        ),
        comment=(
            "Factor model bundle = (prompt + LLM + temp + tools). "
            "MLflow Model Registry mirror."
        ),
    )

    # Partial unique: at most one row active per (factor_id, stage).
    op.create_index(
        "ix_core_factor_models__active",
        "core_factor_models",
        ["factor_id", "stage"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("ix_core_factor_models__active", table_name="core_factor_models")
    op.drop_table("core_factor_models")
