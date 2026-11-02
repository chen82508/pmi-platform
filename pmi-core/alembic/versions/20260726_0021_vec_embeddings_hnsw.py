"""Codify the HNSW ANN index behind SemanticSelector / Tier 0.

Same shape as 0017 §5 (the title-trgm GIN): the index already exists on the EC2
box as an out-of-band manual `CREATE INDEX`, so `IF NOT EXISTS` makes this a
no-op there and a real build everywhere else. Without it in a migration, a fresh
DB answers semantic selection with a full scan of every stored vector.

What it costs to not have it, measured on the box (1.4M vectors, 11 GB table):
`pg_stat_statements` had the SemanticSelector query at 91s mean / 4845s total
across 53 calls — the single largest consumer of DB time on the instance, and
the reason a `score-all` fan-out over the four anchored indexes (war,
semantic-war-demo, btc, sp500) could not finish inside its hourly beat.

Partial + expression, because `vec_market_embeddings.embedding` is an unsized
`vector` so several dimensions can coexist (see the model docstring) and
pgvector will only index a sized one. One index per dimension in use; today the
active model is `ollama/nomic-embed-text` at 768. Adding a 1536-dim model later
means another partial index, not a table rewrite.

`m`/`ef_construction` are pgvector defaults, spelled out so a rebuild elsewhere
matches the box byte-for-byte.

Build cost: minutes-to-hours at production row counts and it takes a
ShareLock on the table (blocks the embed-markets writer, not readers). Fresh
environments build it against an empty table for free. Not
CONCURRENTLY — alembic runs migrations inside a transaction.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_vec_market_embeddings__hnsw_cos_768"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON vec_market_embeddings"
        " USING hnsw ((embedding::vector(768)) vector_cosine_ops)"
        " WITH (m = 16, ef_construction = 64)"
        " WHERE dim = 768"
    )


def downgrade() -> None:
    # Deliberately kept, as with 0017's trgm index: the EC2 copy predates this
    # migration, and dropping it would silently return every semantic selection
    # to a 91s full scan. Drop by hand if a rebuild is really intended.
    pass
