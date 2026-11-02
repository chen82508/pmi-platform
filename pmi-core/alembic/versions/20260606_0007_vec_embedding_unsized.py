"""Unpin vec_market_embeddings.embedding to an unsized pgvector column.

The column shipped as ``vector(1536)`` (OpenAI ``text-embedding-3-small``'s
width). Moving the Tier 0 pre-filter + SemanticSelector to Ollama
``nomic-embed-text`` (768-d) — while keeping the door open to other models —
means rows of different dimensionality must coexist in the same table
(row-per-model design; see the model docstring). pgvector supports an
**unsized** ``vector`` column for exactly this.

Trade-off captured here for posterity: an unsized vector column cannot back an
HNSW/IVFFlat ANN index, so search degrades to an exact cosine seq-scan. That is
acceptable at P0/P1 market counts; the per-dim split / Milvus move is the
scale-triggered follow-up (§3.1 philosophy: add infra only when it breaks).

The table is empty in every environment at this revision (CORR-3.6's writer
lands after this), so the ``ALTER TYPE`` is data-risk-free.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # vector(1536) → vector (unsized). USING cast is a no-op for existing data
    # (none expected) but keeps the statement valid if any row slipped in.
    op.execute(
        "ALTER TABLE vec_market_embeddings "
        "ALTER COLUMN embedding TYPE vector USING embedding::vector"
    )


def downgrade() -> None:
    # Re-pin to 1536. Any row whose dim != 1536 (e.g. a 768-d nomic vector)
    # would make this fail — intentional: you cannot cleanly narrow back to a
    # single dimension once multiple models have written. Truncate first if a
    # downgrade is truly required.
    op.execute(
        "ALTER TABLE vec_market_embeddings "
        "ALTER COLUMN embedding TYPE vector(1536) USING embedding::vector(1536)"
    )
