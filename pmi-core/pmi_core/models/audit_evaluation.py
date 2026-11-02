"""audit_evaluations — immutable factor evaluations.

Lineage:
  (market_id, factor_id, prompt_sha256, model_id, evaluated_at) → unique
Re-running with the same prompt_sha256 + model_id should yield identical output;
that's the reproducibility test in P0 Sprint 3.
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
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class AuditEvaluation(Base):
    __tablename__ = "audit_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_markets.id"), nullable=False
    )
    index_definition_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_index_definitions.id"), nullable=False
    )

    factor_id: Mapped[str] = mapped_column(String(64), nullable=False)

    prompt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_prompts.id"), nullable=False
    )
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # doubles as proof
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Numeric(4, 3))

    # Tier 1 numeric output (e.g. binary 0/1, ternary -1/0/1, or 0..1 score)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(10, 6))
    # ternary "+ / 0 / -" originally, but `type: extraction` factors store a
    # compact JSON label here ({state, party, race_type}) — a real LLM's output
    # exceeds 64 chars for long state names ("Massachusetts"), so widen to 256.
    value_label: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    model_response: Mapped[dict | None] = mapped_column(JSON)  # full LLM response for audit
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # MLflow run that mirrors this evaluation (child run under the pipeline's
    # parent run). Optional: NULL when MLflow was unreachable. Use to look up
    # full LLM trace / tokens / artifacts in the MLflow UI.
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64))

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "market_id",
            "index_definition_id",
            "factor_id",
            "prompt_sha256",
            "model_id",
            name="uq_audit_evaluations__cache_key",
        ),
        {"comment": "APPEND-ONLY. Update = bug. P1 enforces via REVOKE UPDATE."},
    )
