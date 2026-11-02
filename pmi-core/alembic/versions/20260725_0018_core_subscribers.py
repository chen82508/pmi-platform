"""core_subscribers — "Get our updates" weekly-report email subscribers.

Backs the landing page's subscribe form (POST /subscriptions) and the
`weekly-report` worker job. Unsubscribe is a status flip (row kept) keyed by
the opaque `unsubscribe_token` each email's footer link carries.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_subscribers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="active"
        ),
        sa.Column("unsubscribe_token", sa.String(length=64), nullable=False),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="landing"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_core_subscribers__email", "core_subscribers", ["email"]
    )
    op.create_unique_constraint(
        "uq_core_subscribers__unsubscribe_token",
        "core_subscribers",
        ["unsubscribe_token"],
    )


def downgrade() -> None:
    op.drop_table("core_subscribers")
