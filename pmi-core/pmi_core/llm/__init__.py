"""LLM provider abstraction.

Layered above `pmi_core.engine.factor_evaluator` so the evaluator stays a
single function regardless of which model is bound. Implementations live in
sibling modules and are selected by `get_provider(model_id)`.

Currently registered providers:
    - `stub-*`    → `pmi_core.engine.factor_evaluator._stub_score` (in-evaluator)
    - `gpt-*`     → `pmi_core.llm.openai_client.OpenAIProvider` (Tier 1, single-shot)
    - `ollama/*`  → `pmi_core.llm.ollama_client.OllamaProvider` (local models)
    - `agentic/*` → `pmi_core.llm.agentic_client.AgenticProvider` (Tier 2, tool loop)

Adding a new provider:
    1. Drop `pmi_core/llm/<name>_client.py` implementing `LLMProvider`.
    2. Add a `model_id.startswith(...)` → factory branch in `get_provider` (base.py).
    3. No factor / evaluator code changes required.
"""

from pmi_core.llm.base import (
    LLMProvider,
    LLMResponse,
    ParseError,
    UnknownModelError,
    get_provider,
    render_prompt,
)
from pmi_core.llm.embeddings import (
    embed_documents,
    embed_query,
    market_text,
    text_sha256,
)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "ParseError",
    "UnknownModelError",
    "get_provider",
    "render_prompt",
    "embed_documents",
    "embed_query",
    "market_text",
    "text_sha256",
]
