"""core_subscribers — landing-page email subscribers, one row per (email, topic).

Two subscription streams share the table, keyed by `topic`:
  - "new-index"     hero "Get our updates" form → an email whenever a new
                    index goes live (worker job `notify-new-indexes`)
  - "weekly-report" the "Know when the world reprices" card → the Monday
                    digest (worker job `weekly-report`)

Emails are stored lowercase. Unsubscribing flips `status` instead of deleting
the row so a re-subscribe reactivates in place and the original `created_at`
is preserved. `unsubscribe_token` is an opaque per-row secret embedded in that
stream's email footer link — possession of the token is the only auth on that
path (standard newsletter practice; no account system), and it only cancels
the one topic it belongs to.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base

STATUS_ACTIVE = "active"
STATUS_UNSUBSCRIBED = "unsubscribed"

TOPIC_NEW_INDEX = "new-index"
TOPIC_WEEKLY_REPORT = "weekly-report"
TOPICS = frozenset({TOPIC_NEW_INDEX, TOPIC_WEEKLY_REPORT})


class CoreSubscriber(Base):
    __tablename__ = "core_subscribers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # RFC 5321 max address length; normalised to lowercase before insert.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    topic: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TOPIC_WEEKLY_REPORT,
        server_default=TOPIC_WEEKLY_REPORT,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_ACTIVE, server_default=STATUS_ACTIVE
    )
    unsubscribe_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Where the signup came from ("landing" for now; MCP pmi.subscribe later).
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="landing", server_default="landing"
    )

    # Optional profile from the site-v2 subscribe modal (alembic 0027). The
    # hero / footer forms subscribe with an email alone, so all four stay NULL
    # until the reader fills the modal; `interests` is a JSON list of index ids.
    first_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    institution: Mapped[str | None] = mapped_column(String(200))
    interests: Mapped[list[str] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stamped by the sending job after a successful SMTP hand-off.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("email", "topic", name="uq_core_subscribers__email_topic"),
    )
