"""Archive-then-prune bookkeeping for the disk archiver (pmi-archiver).

The archiver exports old snapshot rows to single-table JSON files, uploads them
to Google Drive, and deletes rows from Postgres only after Drive has confirmed
every file of a (table, 30-day window) unit. These tables are the state machine
behind that gate; see docs/reports/disk_monitoring_backup_strategy.md §3.7.

Two invariants are enforced by CHECK constraints, not only by code, so a bug in
the archiver cannot mark a file safe to prune without a matching Drive copy:

* a file is VERIFIED or PRUNED only with a Drive file id and a Drive md5/size
  equal to the local file's;
* only ``role='prune'`` files can ever reach PRUNED (reference and manifest
  files are never deleted from the source tables).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base

UNIT_STATES = (
    "PLANNED",
    "EXPORTING",
    "EXPORTED",
    "UPLOADING",
    "VERIFIED",
    "PRUNING",
    "PRUNED",
    "CLEANED",
)
FILE_STATES = ("LOCAL", "UPLOADED", "VERIFIED", "PRUNED")
FILE_ROLES = ("prune", "reference", "manifest")
RUN_STATUSES = ("running", "succeeded", "halted", "failed")
DRIVE_STATES = ("ACTIVE", "FULL", "RETIRED")
EXPORT_SCOPES = ("prune_set", "full_window")
RESTORE_MODES = ("restore", "verify")
RESTORE_STATUSES = ("running", "succeeded", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CoreArchiveGrid(Base):
    """The 30-day window grid per table. Written once, never recomputed, so
    window boundaries (and therefore archive folders) stay fixed forever."""

    __tablename__ = "core_archive_grid"

    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    # date_trunc('day', min(snapshot_at)) at the time the grid was created.
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("30")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("window_days > 0", name="window_days_positive"),)


class CoreArchiveRun(Base):
    """One archiver invocation (watcher trigger, schedule or manual). Holds the
    before/after disk readings the completion summary reports."""

    __tablename__ = "core_archive_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # diskwatch | nightly | manual | ssm
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'running'")
    )
    prune_enabled: Mapped[bool] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disk_used_pct_before: Mapped[float | None] = mapped_column(Numeric(5, 2))
    disk_used_pct_after: Mapped[float | None] = mapped_column(Numeric(5, 2))
    disk_free_bytes_before: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_bytes_after: Mapped[int | None] = mapped_column(BigInteger)
    units_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    files_uploaded: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    bytes_uploaded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    rows_pruned: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES), name="status_valid"),)


class CoreArchiveUnit(Base):
    """One (table, 30-day window) archive unit, the granularity of the prune gate."""

    __tablename__ = "core_archive_units"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Max source-table id when the unit was planned. Rows inserted later (e.g.
    # backfills into this window) are never in this unit's files, so never
    # deleted by it.
    freeze_max_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Policy time boundary frozen at planning (price: hourly keep-last for rows
    # at or after it, daily before it), so a resumed unit selects the same rows.
    cold_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    export_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    prune_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PLANNED'"))
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_archive_runs.id", ondelete="SET NULL")
    )
    file_count: Mapped[int | None] = mapped_column(Integer)
    rows_exported: Mapped[int | None] = mapped_column(BigInteger)
    bytes_exported: Mapped[int | None] = mapped_column(BigInteger)
    rows_pruned: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("state", UNIT_STATES), name="state_valid"),
        CheckConstraint(_in("export_scope", EXPORT_SCOPES), name="export_scope_valid"),
        CheckConstraint("window_end > window_start", name="window_ordered"),
        # At most one unfinished unit per (table, window); a later sweep unit for
        # the same window may exist once the earlier one is CLEANED.
        Index(
            "uq_core_archive_units__open_window",
            "table_name",
            "window_index",
            unique=True,
            postgresql_where=text("state <> 'CLEANED'"),
        ),
        Index("ix_core_archive_units__state", "state"),
    )


class CoreArchiveFile(Base):
    """One JSON file of a unit: a prune chunk, a core_markets reference chunk,
    or the unit's _manifest.json."""

    __tablename__ = "core_archive_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_archive_units.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)  # the one table this file holds
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    id_min: Mapped[int | None] = mapped_column(BigInteger)
    id_max: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    md5: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    drive_id: Mapped[str | None] = mapped_column(Text)  # shared drive holding the copy
    drive_file_id: Mapped[str | None] = mapped_column(Text)
    drive_md5: Mapped[str | None] = mapped_column(String(32))
    drive_size: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'LOCAL'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("state", FILE_STATES), name="state_valid"),
        CheckConstraint(_in("role", FILE_ROLES), name="role_valid"),
        CheckConstraint("row_count >= 0 AND row_count <= 1000000", name="row_count_range"),
        # The zero-data-loss rule, enforced by the database itself.
        CheckConstraint(
            "state NOT IN ('VERIFIED', 'PRUNED') OR ("
            "drive_file_id IS NOT NULL AND drive_md5 = md5 AND drive_size = size_bytes)",
            name="verified_matches_drive",
        ),
        CheckConstraint("state <> 'PRUNED' OR role = 'prune'", name="only_prune_files_pruned"),
        Index("uq_core_archive_files__unit_file", "unit_id", "file_name", unique=True),
        Index("ix_core_archive_files__unit_state", "unit_id", "state"),
    )


class CoreArchiveDrive(Base):
    """A Google Drive shared drive the archiver writes to. Shared drives cap at
    400,000 items; item_cap leaves headroom and the archiver rolls over."""

    __tablename__ = "core_archive_drives"

    drive_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    item_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("350000"))
    items_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_in("state", DRIVE_STATES), name="state_valid"),
        CheckConstraint("items_used >= 0 AND items_used <= item_cap", name="items_within_cap"),
    )


class CoreArchiveRestore(Base):
    """One restore (or verify-only) of a unit's archived files from Google Drive.

    Restoring does not change the unit's state: the archive stays in Drive and
    the unit stays archived. This is the audit trail of what was read back,
    into which database, and how many rows were inserted or already present.
    """

    __tablename__ = "core_archive_restores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core_archive_units.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core_archive_runs.id", ondelete="SET NULL")
    )
    target_db: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))
    files_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_in_files: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rows_inserted: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rows_skipped: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(_in("mode", RESTORE_MODES), name="mode_valid"),
        CheckConstraint(_in("status", RESTORE_STATUSES), name="status_valid"),
        Index("ix_core_archive_restores__unit", "unit_id"),
    )
