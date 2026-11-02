"""audit_chain_events — append-only chain log consumed by the Polygon indexer.

(tx_hash, log_index) is the natural idempotency key — re-running the indexer
over the same block range is a no-op (ON CONFLICT DO NOTHING). This is the
authoritative trail of WHY any `ts_trades` / `core_traders` / `core_markets`
chain enrichment row exists.

`event_kind` enum (informal — not a DB constraint):
* `ctf_fill`            — CTF Exchange OrderFilled (the trade itself)
* `ctf_transfer`        — ERC-1155 Transfer of outcome token (position change)
* `condition_prepared`  — ConditionalTokens ConditionPreparation (new market)
* `condition_resolved`  — ConditionalTokens ConditionResolution
* `uma_propose`         — UMA Optimistic Oracle V2 ProposePrice
* `uma_dispute`         — UMA Optimistic Oracle V2 DisputePrice
* `uma_settle`          — UMA Optimistic Oracle V2 Settle
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class AuditChainEvent(Base):
    __tablename__ = "audit_chain_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_address: Mapped[str] = mapped_column(String(64), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)

    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_audit_chain_events__tx_log"),
        Index("ix_audit_chain_events__kind_block", "event_kind", "block_number"),
        Index("ix_audit_chain_events__block", "block_number"),
    )
