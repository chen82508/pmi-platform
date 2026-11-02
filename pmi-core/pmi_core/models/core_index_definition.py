"""core_index_definitions — SCD Type 2 PMI definitions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreIndexDefinition(Base):
    """One PMI definition + version. New version = new row, old `effective_to` set."""

    __tablename__ = "core_index_definitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64))

    definition: Mapped[dict] = mapped_column(JSON, nullable=False)  # parsed IR (dict form)
    yaml_source: Mapped[str] = mapped_column(String, nullable=False)
    yaml_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # MLflow experiment that hosts every pipeline-tick run for this index_id.
    # One experiment shared across versions; the version is a run tag, not a
    # separate experiment, so version-on-version comparisons are 1-query in UI.
    mlflow_experiment_id: Mapped[str | None] = mapped_column(String(64))

    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("index_id", "version", name="uq_core_index_definitions__index_id_version"),
        Index(
            "ix_core_index_definitions__current",
            "index_id",
            unique=True,
            postgresql_where="is_current",
        ),
    )
