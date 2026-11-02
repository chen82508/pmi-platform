"""core_subscribers — optional profile fields from the subscribe modal.

The site-v2 subscribe modal ("Tune your reading") asks for first /
last name, an optional institution and the indexes the reader cares about,
so the weekly reading can lead with what matters to them. All four are
nullable: the hero / footer forms still subscribe with an email alone, and the
existing rows carry NULLs. `interests` is a JSON list of index chip ids.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("core_subscribers", sa.Column("first_name", sa.String(length=120), nullable=True))
    op.add_column("core_subscribers", sa.Column("last_name", sa.String(length=120), nullable=True))
    op.add_column("core_subscribers", sa.Column("institution", sa.String(length=200), nullable=True))
    op.add_column(
        "core_subscribers",
        sa.Column("interests", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("core_subscribers", "interests")
    op.drop_column("core_subscribers", "institution")
    op.drop_column("core_subscribers", "last_name")
    op.drop_column("core_subscribers", "first_name")
