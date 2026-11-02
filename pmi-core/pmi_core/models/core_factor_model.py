"""core_factor_models — the (prompt + LLM + temperature + tools) bundle that
the evaluator dereferences at runtime to decide HOW to grade a factor.

YAML index_defs declare WHICH factors an index uses + their default prompt.
A `CoreFactorModel` row, when active, OVERRIDES the YAML's default with an
explicitly-promoted (prompt_id, llm_model_id, temperature, tools_config)
binding. This is the platform's "Model Registry" mirror — MLflow side is
optional via `mlflow_registered_model_name` / `mlflow_model_version`.

Lifecycle:
    1. `pmi-core models register …` inserts a new row with stage='staging',
       is_active=False.
    2. After backtest / canary, `pmi-core models promote <id> --stage production`
       atomically demotes any previous (factor_id, stage='production',
       is_active=True) row and sets this one active.
    3. The evaluator always reads (factor_id, is_active=True, stage='production')
       — exactly one row by partial-unique index. If none, falls back to YAML.

Versioning policy: bumps are append-only. UPDATE only allowed for the two
mutable bits (stage, is_active) to drive promotion. Everything else is
locked at creation time so the (factor_id, version) tuple maps to one
immutable model definition forever.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreFactorModel(Base):
    __tablename__ = "core_factor_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    factor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Model bundle. All four together = the cache key for `audit_evaluations`.
    prompt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_prompts.id"), nullable=False
    )
    llm_model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Numeric(4, 3))
    tools_config: Mapped[dict | None] = mapped_column(JSON)  # P3: agentic tool list

    # Optional MLflow Model Registry link. Same graceful-degradation contract
    # as the rest of the platform: NULL when MLflow unavailable, never blocking.
    mlflow_registered_model_name: Mapped[str | None] = mapped_column(String(128))
    mlflow_model_version: Mapped[str | None] = mapped_column(String(32))

    # Promotion state. `stage` is informational; `is_active` is the gate the
    # evaluator queries. The partial-unique index below makes it impossible to
    # have two active rows for the same (factor_id, stage).
    stage: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'staging'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    description: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("factor_id", "version", name="uq_core_factor_models__factor_version"),
        Index(
            "ix_core_factor_models__active",
            "factor_id",
            "stage",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        {"comment": "Factor model bundle = (prompt + LLM + temp + tools). MLflow Model Registry mirror."},
    )
