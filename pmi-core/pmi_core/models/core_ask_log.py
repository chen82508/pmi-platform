"""core_ask_log — one row per answered "Ask Micah" question.

The ledger behind pmi-api's daily Ask quotas: an anonymous browser may ask
``PMI_API_ASK_ANON_DAILY_QUOTA`` (default 5) questions per US Eastern day, and a
signed-in account ``PMI_API_ASK_USER_DAILY_QUOTA`` (default 10) in total, where
the account's count includes what the same browser asked anonymously that day.
"Asked" means an answered request — presets and routed free-form alike, cached
or not. Requests that never produce a real answer (the disabled notice, the
out-of-scope pointer, a 503 from the answer pipeline) are not charged: /ask
reserves the row before running the agent and deletes it again if no answer
comes back.

A row belongs to an account (``user_id``), an anonymous browser (``anon_id``,
from pmi-web's signed ``micah_anon`` cookie), or both: a signed-in ask records
the browser's anon id too, so signing out doesn't hand out fresh anonymous
questions. The CHECK constraint keeps every row attributable.

Kept as an append-only log rather than a counter column so the charge rules
can evolve without a schema change. Deleting an account's (or anon id's) rows
is the admin way to reset its quota.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreAskLog(Base):
    __tablename__ = "core_ask_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_users.id", ondelete="CASCADE"), nullable=True
    )
    # pmi-web's anonymous browser id (signed `micah_anon` cookie), when known.
    anon_id: Mapped[str | None] = mapped_column(String(64))
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 'preset' | 'freeform'
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The preset id for kind='preset'; NULL for free-form.
    preset_id: Mapped[str | None] = mapped_column(String(64))
    # What was asked (free-form text truncated to fit) — kept for product
    # analytics and for debugging the free-form keyword router.
    question: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        # Named by the metadata convention: ck_core_ask_log__identity.
        CheckConstraint("user_id IS NOT NULL OR anon_id IS NOT NULL", name="identity"),
        Index("ix_core_ask_log__user_time", "user_id", "asked_at"),
        Index(
            "ix_core_ask_log__anon_time",
            "anon_id",
            "asked_at",
            postgresql_where=text("anon_id IS NOT NULL"),
        ),
    )
