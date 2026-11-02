"""The semantic search query must keep the shape the HNSW index can serve.

These assert on compiled SQL rather than on results because the failure they
guard against is silent: drop the cast or the `dim` filter and the query still
returns the right rows, just via a full scan of every stored vector (measured at
91s mean on the live 1.4M-row table, vs sub-second on the index). Nothing
downstream notices — indexes only stop scoring on time.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from pmi_core.vectorstore.pgvector_store import build_search_stmt


def _sql(*, dim: int = 768, min_cosine: float | None = 0.6, top_k: int | None = 250) -> str:
    stmt = build_search_stmt(
        query_embedding=[0.0] * dim,
        model="ollama/nomic-embed-text",
        min_cosine=min_cosine,
        top_k=top_k,
    )
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_casts_to_a_sized_vector() -> None:
    """pgvector only indexes a sized vector; the column is deliberately unsized."""
    assert "CAST(vec_market_embeddings.embedding AS VECTOR(768))" in _sql()


def test_cast_width_follows_the_query_vector() -> None:
    """A 1536-dim model must cast to 1536 — not to whatever index happens to exist."""
    assert "CAST(vec_market_embeddings.embedding AS VECTOR(1536))" in _sql(dim=1536)


def test_filters_on_dim() -> None:
    """The index is partial (`WHERE dim = 768`), unusable without this predicate."""
    assert "vec_market_embeddings.dim = " in _sql()


def test_orders_and_limits() -> None:
    """HNSW serves ordered nearest-neighbour scans, never a bare distance filter."""
    sql = _sql(top_k=250)
    assert "ORDER BY" in sql
    assert "LIMIT" in sql


def test_cosine_floor_becomes_a_distance_bound() -> None:
    """similarity >= min_cosine is expressed as distance <= 1 - min_cosine."""
    assert "<= " in _sql(min_cosine=0.6)
    assert "<= " not in _sql(min_cosine=None).split("ORDER BY")[0].split("WHERE")[1]
