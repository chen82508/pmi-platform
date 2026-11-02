"""audit_pipeline_runs — one row per engine pipeline tick.

Captures cost + counts so weekly digest can render `markets_in / scores_out / cost_usd`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class AuditPipelineRun(Base):
    __tablename__ = "audit_pipeline_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    index_definition_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_index_definitions.id")
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    markets_in: Mapped[int | None] = mapped_column(Integer)
    evaluations_written: Mapped[int | None] = mapped_column(Integer)
    scores_out: Mapped[int | None] = mapped_column(Integer)
    llm_calls: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_message: Mapped[str | None] = mapped_column(String)

    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    # MLflow parent-run id that mirrors this pipeline tick. NULL when MLflow
    # was unreachable. Each child evaluation hangs under this run.
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
