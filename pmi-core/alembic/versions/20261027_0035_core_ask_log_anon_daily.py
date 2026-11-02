"""core_ask_log — anonymous askers and per-day quota lookups.

Ask Micah moves from a lifetime quota per signed-in account (PMI_API_ASK_QUOTA,
20) to daily quotas: anonymous visitors get PMI_API_ASK_ANON_DAILY_QUOTA (5) per
US Eastern day, identified by pmi-web's signed `micah_anon` cookie, and a
signed-in account gets PMI_API_ASK_USER_DAILY_QUOTA (10) in total, counting what
the same browser already used anonymously that day.

So a ledger row now belongs to an account, an anonymous browser, or both (a
signed-in ask also records the browser's anon id, which is what lets the day's
anonymous use count toward the signed-in total, and stops a sign-out from
handing out fresh anonymous questions). `user_id` becomes nullable, a CHECK
keeps every row attributable, and the two indexes serve the
"rows since today's midnight" counts. The table is small (one row per answered
question), so plain index builds are fine.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("core_ask_log", sa.Column("anon_id", sa.String(length=64), nullable=True))
    op.alter_column("core_ask_log", "user_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        op.f("ck_core_ask_log__identity"),
        "core_ask_log",
        "user_id IS NOT NULL OR anon_id IS NOT NULL",
    )
    op.drop_index("ix_core_ask_log__user", table_name="core_ask_log")
    op.create_index("ix_core_ask_log__user_time", "core_ask_log", ["user_id", "asked_at"])
    op.create_index(
        "ix_core_ask_log__anon_time",
        "core_ask_log",
        ["anon_id", "asked_at"],
        postgresql_where=sa.text("anon_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_core_ask_log__anon_time", table_name="core_ask_log")
    op.drop_index("ix_core_ask_log__user_time", table_name="core_ask_log")
    op.create_index("ix_core_ask_log__user", "core_ask_log", ["user_id"])
    op.drop_constraint(op.f("ck_core_ask_log__identity"), "core_ask_log", type_="check")
    # Anonymous-only rows can't satisfy the old NOT NULL; they go with the column.
    op.execute("DELETE FROM core_ask_log WHERE user_id IS NULL")
    op.alter_column("core_ask_log", "user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("core_ask_log", "anon_id")
