"""Embedding path — text → vector, provider-routed by model id.

Mirrors `get_provider`'s prefix dispatch so the embedding model id is configured
exactly like a chat model id:

    ollama/<tag>          → Ollama's OpenAI-compatible /v1/embeddings (free, local)
    text-embedding-* |
    openai/* | gpt-*      → OpenAI (or PMI_LLM_BASE_URL) /v1/embeddings

Asymmetric task prefixes (the gotcha)
-------------------------------------
`nomic-embed-text` is trained for retrieval and REQUIRES task prefixes; without
them cosine scores are systematically off and thresholds (anchor 0.78, Tier 0
0.5) won't calibrate. We apply them automatically:

    stored markets  → "search_document: " + text   (embed_documents)
    query anchor    → "search_query: "    + text   (embed_query)

Models that don't use prefixes (OpenAI's text-embedding-*) get ("", "") and are
embedded verbatim. To add a prefixed model, extend `_EMBEDDING_PREFIXES`.

Cost: Ollama is local → free. OpenAI embeddings are cheap but billed; we don't
track per-call embedding cost yet (the writer logs counts, not dollars).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import structlog

from pmi_core.config import settings

log = structlog.get_logger(__name__)


def market_text(title: str | None, description: str | None) -> str:
    """Canonical text embedded for a market = title + description.

    Single definition so the writer (which computes the cache-key sha) and any
    future re-embed path agree byte-for-byte — otherwise sha256 drift would
    silently re-embed everything.
    """
    title = (title or "").strip()
    description = (description or "").strip()
    return f"{title}\n\n{description}".strip()


def text_sha256(text: str) -> str:
    """Cache-key hash for an embedding input (matches vec_market_embeddings.text_sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# Bare model tag → (document_prefix, query_prefix). Keyed AFTER stripping the
# provider prefix (so "ollama/nomic-embed-text" looks up "nomic-embed-text").
_EMBEDDING_PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
}


def _bare(model: str) -> str:
    """Strip the provider routing prefix to get the model tag the API wants."""
    for p in ("ollama/", "openai/", "local/", "self-hosted/"):
        if model.startswith(p):
            return model[len(p) :]
    return model


def _prefixes(model: str) -> tuple[str, str]:
    return _EMBEDDING_PREFIXES.get(_bare(model), ("", ""))


@lru_cache(maxsize=4)
def _client_for(model: str) -> tuple[Any, str]:
    """Return `(AsyncOpenAI, bare_model)` routed by the model's provider prefix.

    Cached per model id so connection setup is paid once, not per batch.
    """
    from openai import AsyncOpenAI

    bare = _bare(model)
    if model.startswith("ollama/"):
        base_url = settings.ollama_base_url
        if not base_url:
            raise RuntimeError(
                "PMI_OLLAMA_BASE_URL is empty — set it before using an "
                "ollama/* embedding model (default http://localhost:11434/v1, "
                "or http://ollama:11434/v1 inside docker compose)."
            )
        return AsyncOpenAI(base_url=base_url, api_key="ollama-no-auth"), bare

    # OpenAI (or an OpenAI-compatible endpoint via PMI_LLM_BASE_URL).
    api_key = settings.llm_api_key or settings.openai_api_key
    if not api_key:
        raise RuntimeError(
            "No API key for embedding model "
            f"{model!r}: set OPENAI_API_KEY (or PMI_LLM_API_KEY), or use an "
            "ollama/* model for free local embeddings."
        )
    kwargs: dict[str, Any] = {"api_key": api_key}
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return AsyncOpenAI(**kwargs), bare


class EmbeddingsDisabledError(RuntimeError):
    """``PMI_ACTIVE_EMBEDDING_MODEL`` is empty: no embedding service configured."""


async def _embed(texts: Sequence[str], model: str) -> list[list[float]]:
    if not model:
        # Fail fast instead of retrying a service that isn't there; the semantic
        # selector and Tier 0 both fail open on any embedding error.
        raise EmbeddingsDisabledError("embeddings disabled (PMI_ACTIVE_EMBEDDING_MODEL is empty)")
    if not texts:
        return []
    from openai import APIConnectionError, APIError, APIStatusError, RateLimitError
    from tenacity import (
        AsyncRetrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    client, bare = _client_for(model)
    resp: Any = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
        retry=retry_if_exception_type(
            (APIConnectionError, RateLimitError, APIStatusError, APIError)
        ),
        reraise=True,
    ):
        with attempt:
            resp = await client.embeddings.create(model=bare, input=list(texts))
    if resp is None:  # pragma: no cover - reraise=True guarantees a value or raise
        raise RuntimeError("embedding call exhausted retries with no response")
    # Preserve input order (OpenAI guarantees it, but sort defensively).
    items = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in items]


async def embed_documents(
    texts: Sequence[str], model: str | None = None
) -> list[list[float]]:
    """Embed stored documents (markets). Applies the document task prefix."""
    model = model or settings.active_embedding_model
    doc_prefix, _ = _prefixes(model)
    prefixed = [f"{doc_prefix}{t}" for t in texts]
    vecs = await _embed(prefixed, model)
    log.debug("embed.documents", model=model, count=len(vecs))
    return vecs


# Anchor texts are a handful of static YAML strings, but every scoring tick
# embeds each one twice (SemanticSelector + Tier 0 prefilter) — a bounded
# result cache turns those repeat HTTP round-trips into dict hits. Keyed by
# (model, text); tiny cap since only index-def anchors flow through here.
_QUERY_CACHE: dict[tuple[str, str], list[float]] = {}
_QUERY_CACHE_MAX = 64


async def embed_query(text: str, model: str | None = None) -> list[float]:
    """Embed a query/anchor. Applies the query task prefix."""
    model = model or settings.active_embedding_model
    key = (model, text)
    cached = _QUERY_CACHE.get(key)
    if cached is not None:
        return cached
    _, query_prefix = _prefixes(model)
    vecs = await _embed([f"{query_prefix}{text}"], model)
    if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
        _QUERY_CACHE.pop(next(iter(_QUERY_CACHE)))
    _QUERY_CACHE[key] = vecs[0]
    return vecs[0]
