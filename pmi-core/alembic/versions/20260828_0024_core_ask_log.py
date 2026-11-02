"""core_ask_log — per-account "Ask Micah" quota ledger.

One row per answered /ask question (see models/core_ask_log.py for the charge
semantics). pmi-api counts an account's rows against PMI_API_ASK_QUOTA
(default 20) and answers 429 ASK_QUOTA_EXCEEDED once they are spent; deleting
an account's rows resets its quota.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_ask_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("core_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("preset_id", sa.String(length=64), nullable=True),
        sa.Column("question", sa.String(length=512), nullable=True),
    )
    op.create_index("ix_core_ask_log__user", "core_ask_log", ["user_id"])


def downgrade() -> None:
    op.drop_table("core_ask_log")
