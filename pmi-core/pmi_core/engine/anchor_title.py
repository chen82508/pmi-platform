"""LLM-generated titles for By-Question country-group anchors.

A war anchor groups every contract about the same SET of countries (all the
China–Taiwan contracts, all the Iran–US contracts, …). This asks a local LLM
(Ollama gemma — free) for ONE yes/no question that captures the group's central
conflict, in the style "Will China invade Taiwan?" — a natural By-Question card
title. Deterministic-ish (temperature 0) and cached by country-key upstream
(war_snapshot), so it runs only for newly-seen country groups, not per snapshot.

Falls back to a mechanical "China – Taiwan" join whenever the LLM is
unavailable, errors, or returns nothing usable — the feed never blocks on it.
"""

from __future__ import annotations

import json
import re

import structlog

from pmi_core.config import settings

log = structlog.get_logger(__name__)

# Manifold multi-choice umbrella markets prefix every answer with the parent
# market name ("World Conflict Prop Bets 2025-2027 [High Liquidity] — China
# invades Taiwan"), which poisons the embedding (cosine 0.72 vs a clean 0.98).
# Strip the umbrella head so the answer's own question drives the fold.
_UMBRELLA_HEAD_RE = re.compile(
    r"prop bets|mega market|what will happen|high liquidity|\[|"
    r"add your answer|add responses|\bprops?\b|megamarket",
    re.IGNORECASE,
)
# Date / threshold noise that varies across the same question's ladder.
_MONTHS = (
    "january february march april may june july august september october "
    "november december jan feb mar apr jun jul aug sep sept oct nov dec"
).split()
# Modifier words that dress up the same action ("launch an armed invasion" vs
# "invade") — dropped so the core verb+object drives the fold.
_MODIFIERS = {
    "launch", "armed", "military", "forces", "force", "conduct", "offensive",
    "major", "foreign", "operation", "operations", "significantly", "expand",
    "action", "actions", "use", "against", "into", "further", "attempt",
    "sustained", "direct", "full", "scale", "large", "possible", "potential",
}
_STOP = {
    "will", "the", "a", "an", "by", "before", "after", "in", "on", "of", "to",
    "for", "and", "or", "be", "is", "are", "more", "than", "end", "eoy", "least",
    "fewer", "during", "within", "this", "year", "years", "next", "as", "result",
    *_MONTHS,
    *_MODIFIERS,
}

# Map inflected / synonymous surface forms to one canonical token so the same
# question folds regardless of wording: invade/invasion/invading → invade,
# PRC → china, escalation → escalate, etc. Applied after a crude singularise,
# so plural forms (invades, strikes) are covered by the trailing-'s' rule.
_SYNONYMS = {
    "prc": "china",
    "roc": "taiwan",
    "invasion": "invade",
    "invading": "invade",
    "invaded": "invade",
    "attacking": "attack",
    "attacked": "attack",
    "striking": "strike",
    "struck": "strike",
    "annexation": "annex",
    "annexing": "annex",
    "occupation": "occupy",
    "occupying": "occupy",
    "occupied": "occupy",
    "bombing": "bomb",
    "warfare": "war",
    "escalation": "escalate",
    "escalating": "escalate",
    "ceasefires": "ceasefire",
    "nuclear": "nuke",
    "nukes": "nuke",
}


def strip_umbrella_prefix(title: str) -> str:
    """Drop a Manifold umbrella-market prefix: 'Umbrella — Answer' → 'Answer'."""
    if " — " in title:
        head, _, tail = title.partition(" — ")
        if _UMBRELLA_HEAD_RE.search(head):
            return tail.strip()
    return title


def question_tokens(title: str) -> frozenset[str]:
    """The bag of content words a title asks about — a phrasing/date-agnostic key.

    Strips the umbrella prefix, lowercases, drops punctuation/digits, months,
    and stopwords, and singularises (trailing 's'). Two contracts of the same
    question — "Will China invade Taiwan by June 30, 2027?" and a prop's "China
    invades Taiwan" — reduce to the same set {china, invade, taiwan}, so they
    can fold even when their embeddings diverge on the prefix.
    """
    text = strip_umbrella_prefix(title).lower()
    text = re.sub(r"[^a-z\s]", " ", text)  # drop punctuation + digits
    toks = set()
    for w in text.split():
        if w in _STOP or len(w) < 3:
            continue
        if w.endswith("s") and len(w) > 3:  # crude singularise (invades→invade)
            w = w[:-1]
        w = _SYNONYMS.get(w, w)  # canonicalise wording (invasion→invade, prc→china)
        if w in _STOP or len(w) < 3:
            continue
        toks.add(w)
    return frozenset(toks)

DEFAULT_TITLE_MODEL = "gemma3:4b"

# Which way a yes/no war question points: YES = more conflict (+1) or YES =
# less conflict (−1). A folded anchor's LLM headline must point the same way as
# the facing contract it fronts, or the number under it answers the OPPOSITE
# question ("Will Saudi Arabia and the Houthis escalate?" over "Houthi x Saudi
# Arabia effective ceasefire by October 31?", 2026-10-12). Both lists are
# matched as words; a title that hits both (or neither) is ambiguous → None.
_ESCALATE_RE = re.compile(
    r"\b(?:attack|attacks|invade|invades|invasion|strike|strikes|airstrike|bomb|bombs|"
    r"occupy|occupies|occupation|target|targets|escalat\w*|clash|clashes|"
    r"military action|military engagement|declare war|war\b|conflict|troops|seize|annex\w*|"
    r"shell|shelling|blockade|coup|nuclear test|tests?\b|missile|nuke|hostilit\w*|"
    r"collapse|fall\b|falls\b|take\b|takes\b|capture|overthrow|kill\w*|deaths?)",
    re.IGNORECASE,
)
_DEESCALATE_RE = re.compile(
    r"\b(?:ceasefire|cease-fire|truce|armistice|peace|withdraw\w*|lift(?:s|ed|ing)?\b(?: [\w.-]+){0,3} sanctions|"
    r"sanctions relief|deal\b|agreement|accord|resolution|normali[sz]\w*|talks\b|"
    r"negotiat\w*|end the war|end of the war|de-?escalat\w*|demilitari[sz]\w*|"
    r"disarm\w*|reopen\w*|resum\w* diplomatic|lasting)",
    re.IGNORECASE,
)


def title_polarity(title: str | None) -> int | None:
    """+1 if YES on ``title`` means more conflict, −1 if less, None if unclear."""
    if not title:
        return None
    up = bool(_ESCALATE_RE.search(title))
    down = bool(_DEESCALATE_RE.search(title))
    if up == down:
        return None
    return 1 if up else -1


def polarity_matches(title: str | None, facing_direction: int | float | None) -> bool:
    """Whether an LLM headline points the same way as the facing contract's
    audited ``direction`` (+1 escalation / −1 peace). Unknown on either side
    is accepted — only a confident contradiction rejects the headline."""
    if facing_direction is None or facing_direction == 0:
        return True
    pol = title_polarity(title)
    if pol is None:
        return True
    return pol == (1 if facing_direction > 0 else -1)

_SYSTEM = (
    "You label groups of prediction-market contracts about geopolitical "
    "conflict. Return ONLY a JSON object, no prose."
)


def mechanical_title(country_names: list[str]) -> str:
    """The deterministic fallback title — the country names joined."""
    return " – ".join(country_names)  # en dash


async def generate_anchor_title(
    country_names: list[str],
    sample_titles: list[str],
    model: str = DEFAULT_TITLE_MODEL,
) -> str:
    """One yes/no question capturing a country group's central conflict.

    ``country_names`` are display names ("China", "Taiwan"); ``sample_titles``
    are a few of the group's contract titles (highest-relevancy first) as
    grounding. The FIRST sample is the facing contract: the question must
    resolve YES in the same situation it does (same polarity — a ceasefire
    contract gets a ceasefire question, never "escalate?"). Returns the
    mechanical join on any failure; callers still verify polarity
    (``polarity_matches``) and fall back to the contract's own wording.
    """
    base_url = settings.ollama_base_url
    if not base_url or not country_names:
        return mechanical_title(country_names)

    facing = next((t for t in sample_titles if t), None)
    prompt = (
        "These prediction-market contracts are all about the conflict involving "
        f"{', '.join(country_names)}:\n"
        + "\n".join(f"- {t}" for t in sample_titles[:6] if t)
        + "\n\nWrite ONE short yes/no question that captures the central conflict "
        'these contracts share, in the style "Will China invade Taiwan?". '
        'Keep it under 12 words, start with "Will". '
        + (
            f'It must be answered YES in exactly the situation where "{facing}" '
            "resolves YES — keep that contract's polarity (a ceasefire question "
            "stays a ceasefire question; do not flip it into an escalation question). "
            if facing
            else ""
        )
        + 'Return JSON: {"question": "..."}'
    )

    try:
        from pmi_core.llm.ollama_client import _get_async_client

        client = _get_async_client(base_url)
        completion = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = (completion.choices[0].message.content or "").strip()
        question = (json.loads(raw).get("question") or "").strip()
        if question:
            return question
        log.warning("anchor_title.empty", countries=country_names)
    except Exception as e:  # noqa: BLE001 — never block the feed on title gen
        log.warning("anchor_title.llm_failed", countries=country_names, error=str(e))

    return mechanical_title(country_names)
