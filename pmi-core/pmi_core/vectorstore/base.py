"""VectorStore abstraction — the seam that keeps vectors swappable.

The engine (SemanticSelector + Tier 0 pre-filter) and the embed-markets writer
talk to vectors ONLY through this protocol. The concrete store is chosen by
`get_vector_store()` (see __init__). Today that is `PgVectorStore` (pgvector in
the same Postgres — zero new infra, honouring §3.1). When market/vector counts
outgrow an exact cosine seq-scan, `MilvusStore` drops in behind the same three
methods with no change to selector / evaluator / writer.

Why no SQLAlchemy session in the signatures
-------------------------------------------
A session is a Postgres concept; Milvus has none. Keeping it out of the
protocol is what makes the seam real — each implementation manages its own
connection/transaction internally. Reads don't need the caller's transaction
(embeddings are written by a separate job), so this costs nothing.

Cosine convention
-----------------
`cosine` everywhere is cosine *similarity* in [-1, 1] (1 = identical), NOT
pgvector's `<=>` cosine *distance*. PgVectorStore converts (`sim = 1 - dist`)
so callers compare against intuitive thresholds (anchor min_similarity 0.78,
Tier 0 floor 0.5).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class EmbeddingItem:
    """One (market, model) embedding to persist. `dim` is derived from the vector."""

    market_id: int
    model: str
    embedding: list[float]
    text_sha256: str

    @property
    def dim(self) -> int:
        return len(self.embedding)


@dataclass(slots=True)
class ScoredMarket:
    """A market and its cosine similarity to the query vector."""

    market_id: int
    cosine: float


class VectorStore(Protocol):
    async def upsert_many(self, items: Iterable[EmbeddingItem]) -> int:
        """Persist embeddings idempotently on (market_id, model, text_sha256).

        Returns the number of NEW rows written (existing cache-key hits are
        skipped, not overwritten — embeddings are append-only like evaluations).
        """
        ...

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        model: str,
        min_cosine: float | None = None,
        top_k: int | None = None,
    ) -> list[ScoredMarket]:
        """Find markets whose `model` embedding is nearest the query vector.

        Filters to rows of the given `model`. Results sorted by cosine desc.
        `min_cosine` drops anything below the floor; `top_k` caps the count.
        Used by SemanticSelector (discovery).
        """
        ...

    async def cosine_for_markets(
        self,
        *,
        market_ids: Sequence[int],
        query_embedding: Sequence[float],
        model: str,
    ) -> dict[int, float]:
        """Cosine similarity of each given market's `model` embedding vs the query.

        Bounded to an already-selected candidate set — used by the Tier 0 gate
        to decide which keyword/category hits are similar enough to the anchor
        to be worth a factor LLM call. Markets with no embedding row for `model`
        are absent from the result (caller decides fail-open vs fail-closed).
        """
        ...
