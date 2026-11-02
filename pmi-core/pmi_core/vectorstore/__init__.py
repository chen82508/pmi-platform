"""VectorStore package — `get_vector_store()` is the single entry point.

Callers (selector, pipeline Tier 0 gate, embed-markets writer) do:

    from pmi_core.vectorstore import get_vector_store
    store = get_vector_store()
    await store.search(query_embedding=..., model=...)

and never import a concrete store. Swapping pgvector → Milvus is a config flip
(`PMI_VECTOR_STORE=milvus`), not a code change in the callers.
"""

from __future__ import annotations

from functools import lru_cache

from pmi_core.config import settings
from pmi_core.vectorstore.base import EmbeddingItem, ScoredMarket, VectorStore

__all__ = ["EmbeddingItem", "ScoredMarket", "VectorStore", "get_vector_store"]


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    """Return the configured VectorStore singleton (`PMI_VECTOR_STORE`)."""
    name = (settings.vector_store or "pgvector").strip().lower()
    if name == "pgvector":
        from pmi_core.vectorstore.pgvector_store import PgVectorStore

        return PgVectorStore()
    if name == "milvus":
        from pmi_core.vectorstore.milvus_store import MilvusStore

        return MilvusStore()
    raise ValueError(
        f"Unknown PMI_VECTOR_STORE={name!r}. Expected 'pgvector' (default) or 'milvus'."
    )
