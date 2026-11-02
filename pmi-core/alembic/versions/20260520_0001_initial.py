"""Initial pmi-core schema (core_/ts_/audit_/vec_ tiers).

Revision ID: 0001
Revises:
Create Date: 2026-05-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions are created by the docker-entrypoint init SQL on first boot, but
    # add IF NOT EXISTS here so a fresh DB without that init still works.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ────────────────────────── core_* ──────────────────────────
    op.create_table(
        "core_markets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(256)),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("description", sa.String),
        sa.Column("category", sa.String(64)),
        sa.Column("tags", postgresql.JSON()),
        sa.Column("opens_at", sa.DateTime(timezone=True)),
        sa.Column("closes_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.String(32)),
        sa.Column("raw", postgresql.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("venue", "external_id", name="uq_core_markets__venue_external_id"),
        comment="Mutable market identity. New venues do NOT change this schema.",
    )
    op.create_index("ix_core_markets__venue", "core_markets", ["venue"])

    op.create_table(
        "core_index_definitions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("index_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("owner", sa.String(64)),
        sa.Column("definition", postgresql.JSON(), nullable=False),
        sa.Column("yaml_source", sa.String, nullable=False),
        sa.Column("yaml_sha256", sa.String(64), nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("effective_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "index_id", "version", name="uq_core_index_definitions__index_id_version"
        ),
    )
    op.create_index(
        "ix_core_index_definitions__index_id",
        "core_index_definitions",
        ["index_id"],
    )
    op.create_index(
        "ix_core_index_definitions__current",
        "core_index_definitions",
        ["index_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "core_prompts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("template", sa.String, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_core_prompts__name_version"),
    )
    op.create_index("ix_core_prompts__name", "core_prompts", ["name"])
    op.create_index("ix_core_prompts__sha256", "core_prompts", ["sha256"])

    op.create_table(
        "core_api_keys",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("label", sa.String(128)),
        sa.Column("rate_limit_per_minute", sa.Integer, nullable=False, server_default=sa.text("60")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_core_api_keys__key_prefix", "core_api_keys", ["key_prefix"])

    # ────────────────────────── ts_* ──────────────────────────
    op.create_table(
        "ts_price_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "market_id",
            sa.BigInteger,
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_price", sa.Numeric(10, 6)),
        sa.Column("bid", sa.Numeric(10, 6)),
        sa.Column("ask", sa.Numeric(10, 6)),
        sa.Column("volume_24h", sa.Numeric(20, 4)),
        sa.Column("liquidity", sa.Numeric(20, 4)),
    )
    op.create_index(
        "ix_ts_price_snapshots__market_time",
        "ts_price_snapshots",
        ["market_id", "snapshot_at"],
    )

    op.create_table(
        "ts_index_scores",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "index_definition_id",
            sa.BigInteger,
            sa.ForeignKey("core_index_definitions.id"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=False),
        sa.Column("component_count", sa.Integer, nullable=False),
        sa.Column(
            "component_evaluation_ids",
            postgresql.ARRAY(sa.BigInteger),
            nullable=False,
        ),
        sa.Column("breakdown", postgresql.JSON()),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("index_definition_id", "as_of", name="uq_ts_index_scores__defid_asof"),
    )

    # ────────────────────────── audit_* ──────────────────────────
    op.create_table(
        "audit_evaluations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "market_id",
            sa.BigInteger,
            sa.ForeignKey("core_markets.id"),
            nullable=False,
        ),
        sa.Column(
            "index_definition_id",
            sa.BigInteger,
            sa.ForeignKey("core_index_definitions.id"),
            nullable=False,
        ),
        sa.Column("factor_id", sa.String(64), nullable=False),
        sa.Column(
            "prompt_id",
            sa.BigInteger,
            sa.ForeignKey("core_prompts.id"),
            nullable=False,
        ),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("temperature", sa.Numeric(4, 3)),
        sa.Column("value_numeric", sa.Numeric(10, 6)),
        sa.Column("value_label", sa.String(64)),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("model_response", postgresql.JSON()),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "market_id",
            "index_definition_id",
            "factor_id",
            "prompt_sha256",
            "model_id",
            name="uq_audit_evaluations__cache_key",
        ),
        comment="APPEND-ONLY. Update = bug. P1 enforces via REVOKE UPDATE.",
    )

    op.create_table(
        "audit_source_poll_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("records_returned", sa.Integer),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error_class", sa.String(128)),
        sa.Column("error_message", sa.String),
    )
    op.create_index(
        "ix_audit_source_poll_log__source_time",
        "audit_source_poll_log",
        ["source", "polled_at"],
    )
    op.create_index("ix_audit_source_poll_log__source", "audit_source_poll_log", ["source"])

    op.create_table(
        "audit_source_health",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("p95_latency_ms_24h", sa.Integer),
        sa.Column("records_24h", sa.BigInteger),
        sa.Column("expected_records_24h", sa.BigInteger),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "audit_pipeline_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "index_definition_id",
            sa.BigInteger,
            sa.ForeignKey("core_index_definitions.id"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("markets_in", sa.Integer),
        sa.Column("evaluations_written", sa.Integer),
        sa.Column("scores_out", sa.Integer),
        sa.Column("llm_calls", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("error_message", sa.String),
        sa.Column("metadata_json", postgresql.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ────────────────────────── vec_* ──────────────────────────
    op.create_table(
        "vec_market_embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "market_id",
            sa.BigInteger,
            sa.ForeignKey("core_markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("dim", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "market_id", "model", "text_sha256", name="uq_vec_market_embeddings__cache_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("vec_market_embeddings")
    op.drop_table("audit_pipeline_runs")
    op.drop_table("audit_source_health")
    op.drop_table("audit_source_poll_log")
    op.drop_table("audit_evaluations")
    op.drop_table("ts_index_scores")
    op.drop_table("ts_price_snapshots")
    op.drop_table("core_api_keys")
    op.drop_table("core_prompts")
    op.drop_table("core_index_definitions")
    op.drop_table("core_markets")
