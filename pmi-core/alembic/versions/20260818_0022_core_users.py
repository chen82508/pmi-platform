"""core_users — Google SSO accounts.

Backs pmi-api's POST /auth/google upsert (called server-to-server by pmi-web
after it completes the Google OAuth code exchange). `google_sub` is the
identity key; email is indexed but not unique (Google allows it to change
under the same subject). See models/core_user.py for the full semantics.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("picture_url", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_core_users__google_sub", "core_users", ["google_sub"]
    )
    op.create_index("ix_core_users__email", "core_users", ["email"])


def downgrade() -> None:
    op.drop_table("core_users")
