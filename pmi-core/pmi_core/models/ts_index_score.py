"""ts_index_scores — time-series of PMI scores, immutable + lineage to audit_evaluations.

Future P1: hypertable on `as_of`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class TsIndexScore(Base):
    __tablename__ = "ts_index_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    index_definition_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_index_definitions.id"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Nullable so the pipeline can persist "no score this tick" (below
    # min_components, zero relevancy) without leaving a stale value visible
    # in the API — Micah backport, see micah PR #316 / job-executor PR #12.
    score: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    component_evaluation_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False
    )

    breakdown: Mapped[dict | None] = mapped_column(JSON)  # optional per-factor weights, sums, etc.

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "index_definition_id", "as_of", name="uq_ts_index_scores__defid_asof"
        ),
    )
