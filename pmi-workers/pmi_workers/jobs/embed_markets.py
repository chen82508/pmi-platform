"""`embed-markets` job — populate vec_market_embeddings for the active model.

CORR-3.6. Decoupled from scoring: this writes vectors; the scoring pipeline
(SemanticSelector + Tier 0 gate) only reads them. Idempotent via the
(market_id, model, text_sha256) cache key — a re-run after no market edits
writes zero rows; an edited title/description changes the sha and re-embeds
only that market.

Flow
----
1. Load candidate markets (venues from PMI_EMBED_VENUES, default polymarket;
   unresolved) — id + title + description.
2. Compute canonical text + sha per market (shared helpers, so the sha matches
   whatever a future re-embed path would compute).
3. Skip markets that already have a row for (active_model, sha).
4. Embed the rest in batches via the active embedding model (Ollama nomic by
   default — local, free, with the search_document: prefix applied inside
   `embed_documents`).
5. `VectorStore.upsert_many` (append-only).

Cost: zero with the default Ollama model. Runs on the `daily` cron alias once
wired; for now invoke explicitly: `run-job embed-markets`.
"""

from __future__ import annotations

import structlog
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.llm import embed_documents, market_text, text_sha256
from pmi_core.models import CoreMarket, VecMarketEmbedding
from pmi_core.vectorstore import EmbeddingItem, get_vector_store
from sqlalchemy import select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.embed_markets")

_BATCH = 64  # markets per embedding API call


async def _candidates(model: str) -> list[tuple[int, str]]:
    """Return (market_id, text) for markets missing an embedding for `model`.

    Two set queries instead of one EXISTS probe per market: the old per-row
    loop issued O(candidate-count) sequential round-trips every run (hundreds
    of thousands of queries on prod, tens of minutes even when nothing needed
    embedding). Now the existing (market_id, text_sha256) pairs for the model
    are fetched once (join-restricted to the same candidate scope) and the
    skip check is a Python set lookup. The sha itself stays computed in Python
    so it can't drift from what `embed_documents` writes.
    """
    out: list[tuple[int, str]] = []
    async with session_scope() as session:
        candidate_filter = (
            # CORR-3.12: venue scope is config (PMI_EMBED_VENUES,
            # default ["polymarket"]) instead of a hard filter.
            CoreMarket.venue.in_(settings.embed_venues),
            CoreMarket.resolved_at.is_(None),
        )
        rows = (
            await session.execute(
                select(CoreMarket.id, CoreMarket.title, CoreMarket.description).where(
                    *candidate_filter
                )
            )
        ).all()

        have: set[tuple[int, str]] = {
            (mid, sha)
            for mid, sha in (
                await session.execute(
                    select(
                        VecMarketEmbedding.market_id, VecMarketEmbedding.text_sha256
                    )
                    .join(CoreMarket, CoreMarket.id == VecMarketEmbedding.market_id)
                    .where(VecMarketEmbedding.model == model, *candidate_filter)
                )
            ).all()
        }

    for mid, title, description in rows:
        text = market_text(title, description)
        if not text:
            continue
        if (mid, text_sha256(text)) in have:
            continue
        out.append((mid, text))
    return out


@register("embed-markets", queue="long")
async def run() -> None:
    model = settings.active_embedding_model
    if not model:
        log.info("embed_markets.disabled")
        return
    store = get_vector_store()

    pending = await _candidates(model)
    if not pending:
        log.info("embed_markets.nothing_to_do", model=model)
        return

    log.info("embed_markets.start", model=model, pending=len(pending))
    written = 0
    for i in range(0, len(pending), _BATCH):
        chunk = pending[i : i + _BATCH]
        texts = [t for _, t in chunk]
        vectors = await embed_documents(texts, model=model)
        items = [
            EmbeddingItem(
                market_id=mid,
                model=model,
                embedding=vec,
                text_sha256=text_sha256(text),
            )
            for (mid, text), vec in zip(chunk, vectors, strict=True)
        ]
        written += await store.upsert_many(items)
        log.info("embed_markets.batch", done=i + len(chunk), total=len(pending))

    log.info("embed_markets.done", model=model, written=written, candidates=len(pending))
