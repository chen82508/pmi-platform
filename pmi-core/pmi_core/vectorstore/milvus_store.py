"""Milvus-backed VectorStore — drop-in for scale (NOT wired yet).

Intentionally a stub. §3.1 keeps vectors in pgvector until an exact cosine
seq-scan stops being fast enough; this file exists so that day is a config flip
+ filling in three methods, not a refactor of selector / evaluator / writer.

Mapping when it lands
---------------------
* **collection per embedding model** — `vec_market__<model_slug>`, each with its
  own fixed `dim` and an HNSW/IVF index. This is where Milvus genuinely beats
  pgvector: differing dims live in separate collections natively (no unsized
  column, real ANN index per model). "Which model is active" stays config
  (`settings.active_embedding_model`) → which collection `search` targets.
* **fields**: `market_id` (Int64, primary or scalar), `embedding` (FloatVector),
  `text_sha256` (VarChar) for the idempotency key. `core_markets` stays in
  Postgres; join `market_id` in application code (the cost called out when we
  chose this seam — no cross-store SQL join).
* **upsert_many** → `collection.upsert` keyed on (market_id, text_sha256).
* **search / cosine_for_markets** → `collection.search` with `metric_type=COSINE`;
  Milvus returns similarity directly (no 1 - distance conversion needed).

Deps (when activated): `pymilvus`, plus etcd + minio + milvus-standalone in a
`vector` docker-compose profile (or Zilliz Cloud).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pmi_core.vectorstore.base import EmbeddingItem, ScoredMarket

_NOT_WIRED = (
    "MilvusStore is a forward-compatibility stub. Set PMI_VECTOR_STORE=pgvector "
    "(the default) until the Milvus profile + pymilvus integration lands. See "
    "this module's docstring for the collection-per-model mapping."
)


class MilvusStore:
    """Placeholder implementing the VectorStore protocol surface."""

    def __init__(self) -> None:
        raise NotImplementedError(_NOT_WIRED)

    async def upsert_many(self, items: Iterable[EmbeddingItem]) -> int:  # pragma: no cover
        raise NotImplementedError(_NOT_WIRED)

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        model: str,
        min_cosine: float | None = None,
        top_k: int | None = None,
    ) -> list[ScoredMarket]:  # pragma: no cover
        raise NotImplementedError(_NOT_WIRED)

    async def cosine_for_markets(
        self,
        *,
        market_ids: Sequence[int],
        query_embedding: Sequence[float],
        model: str,
    ) -> dict[int, float]:  # pragma: no cover
        raise NotImplementedError(_NOT_WIRED)
