"""core_index_announcements — which index launches have been emailed out.

One row per index_id, written by the `notify-new-indexes` worker job the
first time it sees a current definition for that index_id. Its presence is
what makes the job idempotent: detection is "current defs with no
announcement row", so an index is announced exactly once no matter how often
the cron fires. The 0019 migration backfills every pre-existing index_id so
shipping the feature doesn't announce the whole back catalogue.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreIndexAnnouncement(Base):
    __tablename__ = "core_index_announcements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    announced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # How many subscribers the launch email actually reached (0 = detected
    # with nobody subscribed / SMTP unconfigured — still marked announced).
    notified_subscribers: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
