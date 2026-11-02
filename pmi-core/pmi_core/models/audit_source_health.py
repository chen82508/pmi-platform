"""audit_source_health + audit_source_poll_log — observe each ingest source.

`audit_source_poll_log` is append-only (one row per poll attempt).
`audit_source_health` is row-per-source UPSERTed by the ingest loop after each poll.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class AuditSourcePollLog(Base):
    __tablename__ = "audit_source_poll_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    polled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    records_returned: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String)

    __table_args__ = (Index("ix_audit_source_poll_log__source_time", "source", "polled_at"),)


class AuditSourceHealth(Base):
    __tablename__ = "audit_source_health"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    p95_latency_ms_24h: Mapped[int | None] = mapped_column(Integer)
    records_24h: Mapped[int | None] = mapped_column(BigInteger)
    expected_records_24h: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
