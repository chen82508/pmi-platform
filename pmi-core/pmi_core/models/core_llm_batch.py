"""core_llm_batches — state for OpenAI Batch API submissions (CORR-5.3).

One row per submitted batch. The async Batch API is half price but completes
within 24h, so a row carries everything needed to ingest results later without
re-deriving bindings (which may have changed in the meantime):

* `request_meta` maps each line's `custom_id` →
  `{market_id, factor_id, factor_type, prompt_id, model_id, temperature}` —
  the exact cache-key ingredients the eval must persist under.
* `status` walks: submitted → in_progress → completed/failed/expired →
  ingested. `llm-batch-poll` drives the transitions; ingesting writes
  `audit_evaluations` rows through the same `persist_evaluation` path the
  live pipeline uses (ON CONFLICT-safe; a live eval racing the batch wins
  harmlessly).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreLlmBatch(Base):
    __tablename__ = "core_llm_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="openai", nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(128))  # provider's id
    input_file_id: Mapped[str | None] = mapped_column(String(128))
    output_file_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="submitted", nullable=False)
    index_id: Mapped[str] = mapped_column(String(128), nullable=False)
    index_definition_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingested_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1024))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
