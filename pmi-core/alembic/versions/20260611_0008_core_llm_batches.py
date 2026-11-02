"""core_llm_batches — OpenAI Batch API submission state (CORR-5.3).

Plain OLTP table; one row per submitted batch. `request_meta` JSON maps each
line's custom_id to its cache-key ingredients so ingest never re-derives
bindings that may have changed since submission.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_llm_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="openai"),
        sa.Column("batch_id", sa.String(128)),
        sa.Column("input_file_id", sa.String(128)),
        sa.Column("output_file_id", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
        sa.Column("index_id", sa.String(128), nullable=False),
        sa.Column("index_definition_id", sa.BigInteger(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.String(1024)),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_core_llm_batches__status", "core_llm_batches", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_core_llm_batches__status", table_name="core_llm_batches")
    op.drop_table("core_llm_batches")
