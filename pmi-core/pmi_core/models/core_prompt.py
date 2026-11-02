"""core_prompts — append-only registry of every prompt template ever used.

The same template + version is computed-hashed once (`sha256`) and never overwritten.
Every audit_evaluations row points back to one core_prompts row by id + sha256.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CorePrompt(Base):
    __tablename__ = "core_prompts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # MLflow Prompt Registry URI: 'prompts:/<name>/<mlflow_version>'.
    # NULL when MLflow was unreachable at registration time; backfill via
    # `pmi-core mlflow-init`. NOT used for compliance — sha256 above is the
    # authoritative lineage identifier.
    mlflow_prompt_uri: Mapped[str | None] = mapped_column(String(256))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("name", "version", name="uq_core_prompts__name_version"),)
