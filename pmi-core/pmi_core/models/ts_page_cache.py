"""ts_page_cache — pre-rendered API response blobs (append-only; latest row wins).

One row per (cache_key, computed_at). Readers use:
  SELECT payload FROM ts_page_cache
  WHERE cache_key = ? ORDER BY computed_at DESC LIMIT 1

cache_key conventions:
  "maga:{cycle_year}"                          e.g. "maga:2026"
  "senate-board:{index_id}"                    e.g. "senate-board:us-senate-2026-republican-seats"
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class TsPageCache(Base):
    __tablename__ = "ts_page_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_ts_page_cache__key_time", "cache_key", computed_at.desc()),
        # LIKE-prefix reads can't use the index above under en_US.utf8; this one
        # serves pmi_api.routes.insights._PAST_SQL (alembic 0034). Its predicate
        # must stay identical to that query's WHERE clause.
        Index(
            "ix_ts_page_cache__insight_issue",
            "cache_key",
            computed_at.desc(),
            postgresql_where=text("cache_key LIKE 'insight:issue:%'"),
        ),
    )
