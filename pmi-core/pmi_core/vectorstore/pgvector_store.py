"""pgvector-backed VectorStore over vec_market_embeddings.

`search` is an ANN top-k scan on the per-dimension HNSW index (migration 0021);
`cosine_for_markets` stays exact — it scores a caller-supplied id list, so the
`market_id` btree already bounds it.

Hitting the HNSW index
----------------------
The `embedding` column is unsized so rows of several dimensions can coexist (see
the model docstring), and pgvector can only index a *sized* vector. The index is
therefore an expression index on the cast, partial by dimension:

    ON vec_market_embeddings USING hnsw ((embedding::vector(768)) vector_cosine_ops)
    WHERE dim = 768

Three things must line up or Postgres silently falls back to a full scan of
every vector (measured on the EC2 box: 91s mean, 1.4M rows, 11 GB):

1. the ORDER BY expression is the *cast*, byte-identical to the index's,
2. `WHERE dim = <n>` is present so the partial predicate is provably satisfied,
3. there is an ORDER BY + LIMIT — HNSW only serves ordered nearest-neighbour
   scans, never a bare `WHERE distance <= x`.

`build_search_stmt` builds exactly that shape. Any dimension without a matching
index still answers correctly, just via the old seq-scan plan.

Recall
------
An ANN scan visits `hnsw.ef_search` candidates per layer, so a plain scan can
return fewer rows than `top_k` when a cosine floor rejects most of them.
`hnsw.iterative_scan = strict_order` (pgvector ≥ 0.8) keeps pulling — in exact
distance order — until the LIMIT is met or `hnsw.max_scan_tuples` is exhausted,
which is what makes the floor + top_k contract survivable.

Measured on live data against the exact plan it replaces: top_k 10k → 3.7s, and
war-index's worst case (top_k 20k, the largest any def asks for) → 16.5s, both
returning a full LIMIT. Selectors asking for a few hundred land under a second.

Each method opens its own `session_scope()` — the store owns its transaction so
the protocol stays Postgres-agnostic (see base.py). Reads don't need the
caller's transaction; the writer batches via a single `upsert_many`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import Select

from pmi_core.db import session_scope
from pmi_core.models import VecMarketEmbedding
from pmi_core.vectorstore.base import EmbeddingItem, ScoredMarket

log = structlog.get_logger(__name__)

# Candidates the HNSW walk keeps per layer. pgvector caps the GUC at 1000 and
# recommends ef_search >= LIMIT; top_k is often far above the cap, which is what
# iterative scan is for.
_EF_SEARCH_MAX = 1000
_EF_SEARCH_MIN = 40
# Ceiling on tuples an iterative scan may visit before giving up on filling the
# LIMIT. 4x top_k leaves room for the cosine floor to reject candidates without
# letting a pathological anchor walk the whole index.
_SCAN_TUPLES_PER_HIT = 4
_SCAN_TUPLES_MAX = 200_000


def build_search_stmt(
    *,
    query_embedding: Sequence[float],
    model: str,
    min_cosine: float | None,
    top_k: int | None,
) -> Select:
    """The nearest-neighbour SELECT, in the one shape the HNSW index can serve.

    Split out from `search` so the index-matching contract (cast + `dim` filter
    + ORDER BY/LIMIT) is assertable without a database — breaking it doesn't
    change results, only the plan, so nothing else would catch it.
    """
    q = list(query_embedding)
    dim = len(q)

    # `<=>` cosine distance; similarity = 1 - distance.
    embedding = cast(VecMarketEmbedding.embedding, Vector(dim))
    distance = embedding.cosine_distance(q)
    similarity = (1.0 - distance).label("cosine")

    stmt = (
        select(VecMarketEmbedding.market_id, similarity)
        .where(VecMarketEmbedding.model == model, VecMarketEmbedding.dim == dim)
        .order_by(distance)  # nearest first == highest similarity first
    )
    if min_cosine is not None:
        # distance <= 1 - min_cosine  ⇔  similarity >= min_cosine
        stmt = stmt.where(distance <= (1.0 - min_cosine))
    if top_k is not None:
        stmt = stmt.limit(top_k)
    return stmt


class PgVectorStore:
    """VectorStore implementation over the `vec_market_embeddings` table."""

    async def upsert_many(self, items: Iterable[EmbeddingItem]) -> int:
        rows = [
            {
                "market_id": it.market_id,
                "model": it.model,
                "dim": it.dim,
                "embedding": list(it.embedding),
                "text_sha256": it.text_sha256,
            }
            for it in items
        ]
        if not rows:
            return 0

        async with session_scope() as session:
            stmt = (
                pg_insert(VecMarketEmbedding)
                .values(rows)
                # Append-only: an unchanged (market, model, text) never rewrites.
                .on_conflict_do_nothing(
                    constraint="uq_vec_market_embeddings__cache_key"
                )
                .returning(VecMarketEmbedding.id)
            )
            result = await session.execute(stmt)
            written = len(result.scalars().all())

        log.debug("pgvector.upsert_many", submitted=len(rows), written=written)
        return written

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        model: str,
        min_cosine: float | None = None,
        top_k: int | None = None,
    ) -> list[ScoredMarket]:
        q = list(query_embedding)
        stmt = build_search_stmt(
            query_embedding=q, model=model, min_cosine=min_cosine, top_k=top_k
        )
        async with session_scope() as session:
            await self._tune_ann_scan(session, top_k=top_k)
            result = await session.execute(stmt)
            return [ScoredMarket(market_id=mid, cosine=float(cos)) for mid, cos in result.all()]

    @staticmethod
    async def _tune_ann_scan(session, *, top_k: int | None) -> None:
        """Size this transaction's HNSW walk to the top_k being asked for.

        `SET LOCAL` so the settings die with the transaction and never leak into
        a pooled connection's next caller. Best-effort: a Postgres without
        pgvector ≥ 0.8 rejects the GUCs, and the query is still *correct*
        without them (just planned as a full scan), so a failure here must not
        abort the search. The rollback is what keeps the aborted transaction
        usable for the SELECT that follows.
        """
        want = top_k or _EF_SEARCH_MIN
        ef_search = max(_EF_SEARCH_MIN, min(want, _EF_SEARCH_MAX))
        max_scan = min(max(want * _SCAN_TUPLES_PER_HIT, 20_000), _SCAN_TUPLES_MAX)
        try:
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
            await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
            await session.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {max_scan}"))
        except Exception as exc:  # noqa: BLE001 - tuning is optional, results aren't
            await session.rollback()
            log.debug("pgvector.ann_tuning_unavailable", error=str(exc))

    async def cosine_for_markets(
        self,
        *,
        market_ids: Sequence[int],
        query_embedding: Sequence[float],
        model: str,
    ) -> dict[int, float]:
        ids = list(market_ids)
        if not ids:
            return {}
        q = list(query_embedding)
        similarity = (1.0 - VecMarketEmbedding.embedding.cosine_distance(q)).label("cosine")

        stmt = select(VecMarketEmbedding.market_id, similarity).where(
            VecMarketEmbedding.model == model,
            VecMarketEmbedding.market_id.in_(ids),
        )
        async with session_scope() as session:
            result = await session.execute(stmt)
            # A market could (in theory) have multiple cache-key rows for one
            # model (different text_sha256 after an edit). Keep the best.
            best: dict[int, float] = {}
            for mid, cos in result.all():
                c = float(cos)
                if mid not in best or c > best[mid]:
                    best[mid] = c
            return best
