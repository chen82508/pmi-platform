"""Split subscriptions into topics + track announced index launches.

The landing page has TWO subscribe forms with different meanings: the hero
"Get our updates" = an email whenever a NEW index goes live; the "Know when
the world reprices" card = the Monday weekly digest. 0018 modelled a single
list — this adds `core_subscribers.topic` (unique per (email, topic), so one
address can hold both subscriptions) and `core_index_announcements`, the
announced-once ledger the `notify-new-indexes` job checks against.

Backfill: every index_id that already has a definition is inserted as
already-announced so the first job run doesn't email the entire back
catalogue as "new".

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "core_subscribers",
        sa.Column(
            "topic",
            sa.String(length=32),
            nullable=False,
            server_default="weekly-report",
        ),
    )
    op.drop_constraint(
        "uq_core_subscribers__email", "core_subscribers", type_="unique"
    )
    op.create_unique_constraint(
        "uq_core_subscribers__email_topic", "core_subscribers", ["email", "topic"]
    )

    op.create_table(
        "core_index_announcements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("index_id", sa.String(length=200), nullable=False),
        sa.Column(
            "announced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "notified_subscribers",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_unique_constraint(
        "uq_core_index_announcements__index_id",
        "core_index_announcements",
        ["index_id"],
    )
    # Pre-existing indexes are not "new" — mark them announced at cutover.
    op.execute(
        "INSERT INTO core_index_announcements (index_id, announced_at) "
        "SELECT DISTINCT index_id, now() FROM core_index_definitions"
    )


def downgrade() -> None:
    op.drop_table("core_index_announcements")
    # Collapsing back to one-row-per-email can violate the old unique(email);
    # keep the first (lowest-id) row per address, matching 0018 semantics.
    op.execute(
        "DELETE FROM core_subscribers s USING core_subscribers keep "
        "WHERE s.email = keep.email AND s.id > keep.id"
    )
    op.drop_constraint(
        "uq_core_subscribers__email_topic", "core_subscribers", type_="unique"
    )
    op.create_unique_constraint(
        "uq_core_subscribers__email", "core_subscribers", ["email"]
    )
    op.drop_column("core_subscribers", "topic")
