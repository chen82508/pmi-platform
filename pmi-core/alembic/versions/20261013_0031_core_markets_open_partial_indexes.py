"""Partial indexes over the open slice of core_markets (2026-10-13 ingest stall).

`core_markets` is 8.8M rows / 19 GB heap, but only ~8% of rows are unresolved,
and almost every hot reader asks for exactly that slice: the polymarket-ws token
loader (`venue = ? AND resolution IS NULL AND closes_at > now() ORDER BY
closes_at DESC NULLS LAST`), the CLOB pollers' eligible-market selects, and the
board / landing queries (`resolved_at IS NULL AND (closes_at IS NULL OR
closes_at > now()) AND venue ...`). With no index on any of those columns each
call was a full 19 GB seq scan. On the box, `pg_stat_statements` had the WS
loader alone at 49.7 TB read / 155 s mean across 25.7k calls; the disk sat at
its IOPS ceiling (PSI io full avg300 ≈ 54%), polymarket-rest cycles stretched to
~50 min, the WS dropped on keepalive timeouts every ~25 min, and every default
-lane job (refresh-market-priority included) queued 20–85 min behind slow board
snapshots.

Two indexes because the readers split on predicate: pollers filter
`resolution IS NULL`, boards filter `resolved_at IS NULL`, and the planner only
uses a partial index whose predicate the query implies.

Column choice keeps HOT updates intact: `venue` / `closes_at` / the predicate
columns change only on listing edits or resolution. `last_price_at` and
`last_volume_24h` are deliberately NOT indexed — the 0017 price trigger rewrites
them every heartbeat, and indexing them would make every one of those updates
non-HOT (the reason 0017 dropped `ix_core_markets__last_price_at`).

Built CONCURRENTLY inside an autocommit block so a production apply never takes
a ShareLock on the hottest table. Already built out-of-band on the EC2 box during
the incident, so `IF NOT EXISTS` makes this a no-op there. Caveat: a failed
concurrent build leaves an INVALID index that `IF NOT EXISTS` will then skip —
check `pg_index.indisvalid` and DROP/rebuild by hand if a build was interrupted.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "ix_core_markets__open_venue_closes": "resolution IS NULL",
    "ix_core_markets__live_venue_closes": "resolved_at IS NULL",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, predicate in _INDEXES.items():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON core_markets"
                f" (venue, closes_at DESC NULLS LAST) WHERE {predicate}"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
