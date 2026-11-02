"""LLM provider abstraction — protocol + dispatcher + prompt renderer + parser.

Design notes
------------
* `LLMResponse` is **structured** — every provider populates the same fields so
  `factor_evaluator` writes a single shape into `audit_evaluations` regardless
  of vendor. The raw provider payload survives as `LLMResponse.raw_response`.
* `get_provider(model_id)` dispatches by prefix (cheaper than registry lookup
  per evaluation). Real-LLM model ids match `gpt-*`, `claude-*`, etc.; the
  `stub-*` family is intentionally NOT routed here — `factor_evaluator` keeps
  its in-process deterministic stub for `stub-*` to avoid construction cost.
* `parse_factor_response` enforces the prompt-as-JSON contract centrally;
  every prompt under `pmi_core/prompts/factors/` returns the same JSON shape
  (`{value, confidence, reasoning}`), so this helper is shared.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pmi_core.dsl.ir import FactorSpec

# ─── Errors ────────────────────────────────────────────────────────────────


class UnknownModelError(ValueError):
    """Raised when no provider can be matched for a model id."""


class ParseError(ValueError):
    """Raised when the LLM response can't be parsed into a factor value.

    Carries the raw text for debugging so the caller can decide whether to
    log+skip, retry with a different prompt, or escalate.
    """

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text


# ─── Response envelope ─────────────────────────────────────────────────────


@dataclass(slots=True)
class LLMResponse:
    """Provider-agnostic factor evaluation result.

    `value_numeric` / `value_label` already follow the factor-type conventions:

    | factor.type | value_numeric | value_label                              |
    |-------------|---------------|------------------------------------------|
    | binary      | 0.0 or 1.0    | None                                     |
    | ternary     | -1.0, 0.0, 1.0| "-", "0", "+"                            |
    | score       | 0.0..1.0      | None                                     |
    | extraction  | 0.0 or 1.0    | canonical JSON of extracted fields, or   |
    |             | (did extract) | None when value is 0                     |
    """

    model_id: str
    value_numeric: float
    value_label: str | None
    confidence: float
    rationale: str | None
    raw_text: str
    raw_response: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


# ─── Provider protocol ─────────────────────────────────────────────────────


class LLMProvider(Protocol):
    """Single-method protocol every LLM client implements."""

    model_id: str

    async def evaluate(
        self,
        *,
        rendered_prompt: str,
        factor: FactorSpec,
        temperature: float | None = None,
        market: Any | None = None,
        tools_config: dict | None = None,
    ) -> LLMResponse:
        """Run the rendered prompt through the LLM and return a normalised result.

        Implementations MUST:
          - obey `temperature` when not None
          - parse the JSON envelope via `parse_factor_response`
          - populate token counts + `cost_usd` from the provider's billing meter
          - raise `ParseError` if the response can't be coerced into the expected shape

        `market` and `tools_config` are Tier 2 (agentic) context: single-shot
        providers (OpenAI/Ollama) accept and ignore them; `AgenticProvider` binds
        its tools to `market` and reads its tool list / budget from `tools_config`.
        """
        ...


# ─── Prompt rendering ──────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render_prompt(template: str, market: Any) -> str:
    """Substitute `{market_*}` placeholders from a `CoreMarket` (or any dict-like).

    Uses a regex pass instead of `str.format` so unknown placeholders (e.g.
    `{value}` inside JSON shape examples in the prompt body) survive untouched.
    The variables exposed mirror what factor prompts actually use today:

      - `market_title`        ← `market.title`
      - `market_description`  ← `market.description`
      - `market_category`     ← `market.category`
      - `market_tags`         ← comma-joined `market.tags`
      - `market_venue`        ← `market.venue`
      - `market_slug`         ← `market.slug`
      - `market_external_id`  ← `market.external_id`

    Anything else is left as-is. To add a placeholder, just extend the
    `provided` dict — no prompt change required.
    """

    def _get(name: str) -> Any:
        return getattr(market, name, None) if not isinstance(market, dict) else market.get(name)

    tags = _get("tags")
    if isinstance(tags, (list, tuple)):
        tags_str = ", ".join(str(t) for t in tags)
    elif tags is None:
        tags_str = ""
    else:
        tags_str = str(tags)

    provided: dict[str, str] = {
        "market_title": str(_get("title") or ""),
        "market_description": str(_get("description") or ""),
        "market_category": str(_get("category") or ""),
        "market_tags": tags_str,
        "market_venue": str(_get("venue") or ""),
        "market_slug": str(_get("slug") or ""),
        "market_external_id": str(_get("external_id") or ""),
    }

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in provided:
            return provided[key]
        return match.group(0)  # leave unknown placeholders alone

    return _PLACEHOLDER_RE.sub(_sub, template)


# ─── Batch prompt splitting (multi-market eval) ────────────────────────────

_MARKET_PH_RE = re.compile(r"\{market_[a-zA-Z0-9_]*\}")


def split_market_template(template: str) -> tuple[str, str, str] | None:
    """Split a single-market factor template into (head, market_block, tail).

    Batching N markets in one prompt only saves compute if the static rubric
    (the bulk of the template) is sent ONCE and only the per-market data
    repeats. This finds the contiguous span of lines carrying `{market_*}`
    placeholders:

      * **head**  — everything before the first market-placeholder line (the
        rubric: instructions, JSON shape, rules). Sent once per batch.
      * **market_block** — the placeholder lines themselves (e.g.
        ``Title: {market_title}`` / ``Description: {market_description}``).
        Rendered once PER market.
      * **tail**  — anything after (e.g. "Return the JSON only"). Dropped from
        the batch prompt because the caller supplies an array-shaped
        instruction instead; kept only for reference.

    Returns ``None`` when the template has no `{market_*}` placeholder (can't
    be batched) — the caller then falls back to per-market calls. Robust to
    whether the template uses a ``## Market`` heading (only ~half do).
    """
    if not _MARKET_PH_RE.search(template):
        return None
    lines = template.splitlines()
    ph_idx = [i for i, ln in enumerate(lines) if _MARKET_PH_RE.search(ln)]
    start, end = ph_idx[0], ph_idx[-1]
    head = "\n".join(lines[:start]).strip()
    market_block = "\n".join(lines[start : end + 1]).strip()
    tail = "\n".join(lines[end + 1 :]).strip()
    if not head or not market_block:
        return None
    return head, market_block, tail


def parse_factor_batch(
    text: str, factor: FactorSpec, n: int
) -> list[tuple[float, str | None, float, str | None]]:
    """Parse a batched ``{"results":[{"i":idx, "value":..., ...}, ...]}`` envelope.

    Returns a list of ``(value_numeric, value_label, confidence, rationale)``
    of length ``n``, indexed by the original market position. Each element is
    validated by reusing :func:`parse_factor_response` (so all factor-type
    coercion — binary/ternary/score — is identical to the single-call path).

    Raises ``ParseError`` (→ caller falls back to per-market calls) on ANY
    misalignment: not a results array, wrong count, missing/duplicate/out-of-
    range index. Never returns a partially-aligned result — a silent
    off-by-one would corrupt the audit record, so we fail closed.
    """
    payload = _extract_json(text)
    results = payload.get("results")
    if not isinstance(results, list):
        raise ParseError(f"batch missing 'results' array in {str(payload)[:200]}", raw_text=text)
    if len(results) != n:
        raise ParseError(f"batch expected {n} results, got {len(results)}", raw_text=text)

    by_index: dict[int, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict) or "i" not in item:
            raise ParseError(f"batch item missing 'i': {item!r}", raw_text=text)
        try:
            idx = int(item["i"])
        except (TypeError, ValueError) as e:
            raise ParseError(f"batch item 'i' not an int: {item['i']!r}", raw_text=text) from e
        if idx < 0 or idx >= n or idx in by_index:
            raise ParseError(f"batch item index out of range / duplicate: {idx}", raw_text=text)
        by_index[idx] = item

    if len(by_index) != n:
        raise ParseError(f"batch indices not a full 0..{n - 1} cover", raw_text=text)

    out: list[tuple[float, str | None, float, str | None]] = []
    for i in range(n):
        # Reuse the single-response validator by re-serialising the item.
        out.append(parse_factor_response(json.dumps(by_index[i], ensure_ascii=False), factor))
    return out


# ─── Response parsing ──────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from an LLM response.

    Accepts:
      1. Pure JSON object
      2. Object wrapped in ```json fences```
      3. Object embedded mid-text (greedy first {...} match)
    """
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ParseError("response is not JSON", raw_text=text)


def parse_factor_response(
    text: str, factor: FactorSpec
) -> tuple[float, str | None, float, str | None]:
    """Validate LLM JSON against the factor.type contract.

    Returns ``(value_numeric, value_label, confidence, rationale)``.
    Raises ``ParseError`` on shape violation so the caller can retry.
    """
    payload = _extract_json(text)

    if "value" not in payload:
        raise ParseError(f"missing 'value' key in {payload}", raw_text=text)

    raw_value = payload["value"]
    rationale = payload.get("reasoning") or payload.get("rationale")
    confidence_raw = payload.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.7
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))

    if factor.type == "binary":
        try:
            v = float(raw_value)
        except (TypeError, ValueError) as e:
            raise ParseError(f"binary factor expects 0/1, got {raw_value!r}", raw_text=text) from e
        if v not in (0.0, 1.0):
            v = 1.0 if v >= 0.5 else 0.0
        return v, None, confidence, rationale

    if factor.type == "extraction":
        # value = 0/1 "did extract"; when 1, every other non-meta key of the
        # payload is the extracted structure, canonicalised (sorted keys) into
        # value_label so downstream consumers and the cache see a stable
        # string. Domain validation (e.g. "is this a real US state") is the
        # CONSUMER's job — this layer stays generic.
        try:
            v = float(raw_value)
        except (TypeError, ValueError) as e:
            raise ParseError(
                f"extraction factor expects value 0/1, got {raw_value!r}", raw_text=text
            ) from e
        v = 1.0 if v >= 0.5 else 0.0
        if v == 0.0:
            return 0.0, None, confidence, rationale
        fields = {
            k: payload[k]
            for k in payload
            if k not in ("value", "confidence", "reasoning", "rationale")
        }
        if not fields:
            raise ParseError(
                "extraction factor returned value=1 with no extracted fields", raw_text=text
            )
        label = json.dumps(fields, sort_keys=True, ensure_ascii=False)
        return 1.0, label, confidence, rationale

    if factor.type == "ternary":
        try:
            v = float(raw_value)
        except (TypeError, ValueError) as e:
            raise ParseError(f"ternary factor expects -1/0/1, got {raw_value!r}", raw_text=text) from e
        if v >= 0.5:
            v_int = 1.0
        elif v <= -0.5:
            v_int = -1.0
        else:
            v_int = 0.0
        label = {-1.0: "-", 0.0: "0", 1.0: "+"}[v_int]
        return v_int, label, confidence, rationale

    # score
    try:
        v = float(raw_value)
    except (TypeError, ValueError) as e:
        raise ParseError(f"score factor expects 0..1, got {raw_value!r}", raw_text=text) from e
    return max(0.0, min(1.0, v)), None, confidence, rationale


# ─── Provider dispatch ─────────────────────────────────────────────────────


def get_provider(model_id: str) -> LLMProvider:
    """Return an LLMProvider for `model_id`.

    Currently dispatches by prefix. Each provider implementation is imported
    lazily so an environment without (e.g.) the openai SDK installed can still
    import this module for type checking.

    Raises `UnknownModelError` if no provider matches — callers should let
    this bubble up so misconfigured CoreFactorModel rows fail loudly instead
    of silently falling back to the stub.
    """
    if not model_id:
        raise UnknownModelError("model_id is empty")
    # `local/` and `self-hosted/` route to the same OpenAI-compatible provider;
    # the actual endpoint comes from PMI_LLM_BASE_URL in settings. This lets a
    # future self-hosted LLM/ML server be a config flip + a `local/<model>`
    # CoreFactorModel row, with no new provider class.
    # `ollama/<model>` routes to a local Ollama worker (own endpoint, free,
    # coexists with OpenAI-direct) — see PMI_OLLAMA_BASE_URL in settings.
    # `ensemble/<m1>+<m2>` (CORR-5.9) → multi-model voting; each member id is
    # itself routed back through get_provider. Checked FIRST because member
    # ids contain the other prefixes.
    if model_id.startswith("ensemble/"):
        from pmi_core.llm.ensemble_client import EnsembleProvider

        return EnsembleProvider(model_id=model_id)
    # `agentic/<base_model>` (e.g. `agentic/gpt-4o`) → Tier 2 multi-step
    # tool-calling provider. Checked before the gpt-/openai- prefixes so the
    # base model name inside the id doesn't get routed to the single-shot
    # OpenAIProvider. The agent loop reuses OpenAIProvider's transport.
    if model_id.startswith("agentic/"):
        from pmi_core.llm.agentic_client import AgenticProvider

        return AgenticProvider(model_id=model_id)
    if model_id.startswith("ollama/"):
        from pmi_core.llm.ollama_client import OllamaProvider

        return OllamaProvider(model_id=model_id)
    if model_id.startswith(("gpt-", "openai/", "local/", "self-hosted/")):
        from pmi_core.llm.openai_client import OpenAIProvider

        return OpenAIProvider(model_id=model_id)
    # `fixture/<name>` replays the labels carried by fixture markets (demo/CI).
    if model_id.startswith("fixture/"):
        from pmi_core.llm.fixture_client import FixtureProvider

        return FixtureProvider(model_id=model_id)
    raise UnknownModelError(
        f"no provider registered for model_id={model_id!r}. "
        f"Known prefixes: agentic/*, gpt-*, openai/*, local/*, self-hosted/*, "
        f"ollama/*, fixture/*. (stub-* is handled in-evaluator, not via get_provider.)"
    )
