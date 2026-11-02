"""core_archive_* — bookkeeping for the disk archiver (archive-then-prune).

Six small tables behind the zero-data-loss prune gate and the restore audit: the frozen 30-day window
grid per table, archiver runs (with before/after disk readings for the Slack
completion summary), (table, window) units, per-file checksums + Google Drive
confirmations, shared-drive capacity, and restores read back from Drive. Two CHECK constraints enforce the
safety rule in the database itself: a file is VERIFIED/PRUNED only with a Drive
copy whose md5 and size equal the local file's, and only prune files can be
PRUNED. See models/core_archive.py and
docs/reports/disk_monitoring_backup_strategy.md §3.7.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-19

NOTE (merge coordination): 0032 is taken by core_market_esports_tag, which is
applied on the data box but not yet committed to main. If it lands first, rechain
this revision onto 0032 before merging so there is a single head.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ck(table: str, name: str, sql: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sql, name=op.f(f"ck_{table}__{name}"))


def upgrade() -> None:
    op.create_table(
        "core_archive_grid",
        sa.Column("table_name", sa.Text(), primary_key=True),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_days", sa.SmallInteger(), nullable=False, server_default=sa.text("30")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _ck("core_archive_grid", "window_days_positive", "window_days > 0"),
    )

    op.create_table(
        "core_archive_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("prune_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disk_used_pct_before", sa.Numeric(5, 2), nullable=True),
        sa.Column("disk_used_pct_after", sa.Numeric(5, 2), nullable=True),
        sa.Column("disk_free_bytes_before", sa.BigInteger(), nullable=True),
        sa.Column("disk_free_bytes_after", sa.BigInteger(), nullable=True),
        sa.Column("units_completed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("files_uploaded", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bytes_uploaded", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_pruned", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ck(
            "core_archive_runs",
            "status_valid",
            "status IN ('running', 'succeeded', 'halted', 'failed')",
        ),
    )

    op.create_table(
        "core_archive_units",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freeze_max_id", sa.BigInteger(), nullable=False),
        sa.Column("cold_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_scope", sa.String(length=16), nullable=False),
        sa.Column("prune_policy", sa.String(length=32), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default=sa.text("'PLANNED'")
        ),
        sa.Column(
            "last_run_id",
            sa.BigInteger(),
            sa.ForeignKey("core_archive_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("rows_exported", sa.BigInteger(), nullable=True),
        sa.Column("bytes_exported", sa.BigInteger(), nullable=True),
        sa.Column("rows_pruned", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _ck(
            "core_archive_units",
            "state_valid",
            "state IN ('PLANNED', 'EXPORTING', 'EXPORTED', 'UPLOADING', 'VERIFIED', 'PRUNING', 'PRUNED', 'CLEANED')",
        ),
        _ck(
            "core_archive_units",
            "export_scope_valid",
            "export_scope IN ('prune_set', 'full_window')",
        ),
        _ck("core_archive_units", "window_ordered", "window_end > window_start"),
    )
    op.create_index(
        "uq_core_archive_units__open_window",
        "core_archive_units",
        ["table_name", "window_index"],
        unique=True,
        postgresql_where=sa.text("state <> 'CLEANED'"),
    )
    op.create_index("ix_core_archive_units__state", "core_archive_units", ["state"])

    op.create_table(
        "core_archive_files",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "unit_id",
            sa.BigInteger(),
            sa.ForeignKey("core_archive_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("id_min", sa.BigInteger(), nullable=True),
        sa.Column("id_max", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("drive_id", sa.Text(), nullable=True),
        sa.Column("drive_file_id", sa.Text(), nullable=True),
        sa.Column("drive_md5", sa.String(length=32), nullable=True),
        sa.Column("drive_size", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default=sa.text("'LOCAL'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
        _ck(
            "core_archive_files",
            "state_valid",
            "state IN ('LOCAL', 'UPLOADED', 'VERIFIED', 'PRUNED')",
        ),
        _ck("core_archive_files", "role_valid", "role IN ('prune', 'reference', 'manifest')"),
        _ck("core_archive_files", "row_count_range", "row_count >= 0 AND row_count <= 1000000"),
        _ck(
            "core_archive_files",
            "verified_matches_drive",
            "state NOT IN ('VERIFIED', 'PRUNED') OR ("
            "drive_file_id IS NOT NULL AND drive_md5 = md5 AND drive_size = size_bytes)",
        ),
        _ck("core_archive_files", "only_prune_files_pruned", "state <> 'PRUNED' OR role = 'prune'"),
    )
    op.create_index(
        "uq_core_archive_files__unit_file",
        "core_archive_files",
        ["unit_id", "file_name"],
        unique=True,
    )
    op.create_index("ix_core_archive_files__unit_state", "core_archive_files", ["unit_id", "state"])

    op.create_table(
        "core_archive_drives",
        sa.Column("drive_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("item_cap", sa.Integer(), nullable=False, server_default=sa.text("350000")),
        sa.Column("items_used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default=sa.text("'ACTIVE'")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        _ck("core_archive_drives", "state_valid", "state IN ('ACTIVE', 'FULL', 'RETIRED')"),
        _ck(
            "core_archive_drives", "items_within_cap", "items_used >= 0 AND items_used <= item_cap"
        ),
    )

    op.create_table(
        "core_archive_restores",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "unit_id",
            sa.BigInteger(),
            sa.ForeignKey("core_archive_units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("core_archive_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_db", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("files_verified", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_in_files", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_inserted", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rows_skipped", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ck("core_archive_restores", "mode_valid", "mode IN ('restore', 'verify')"),
        _ck("core_archive_restores", "status_valid", "status IN ('running', 'succeeded', 'failed')"),
    )
    op.create_index("ix_core_archive_restores__unit", "core_archive_restores", ["unit_id"])


def downgrade() -> None:
    op.drop_table("core_archive_restores")
    op.drop_table("core_archive_drives")
    op.drop_table("core_archive_files")
    op.drop_table("core_archive_units")
    op.drop_table("core_archive_runs")
    op.drop_table("core_archive_grid")
