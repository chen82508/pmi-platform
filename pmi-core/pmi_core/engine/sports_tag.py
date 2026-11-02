"""sports_tag — LLM classification of a sports market's canonical identity.

Thin wrapper over the shared LLM primitive (`get_provider(...).evaluate`): render
the game-tag-extraction prompt for one market, parse the extraction envelope, and
coerce it into a `core_market_game_tag` row. No IndexDef, no factor registry —
the `tag-games` job calls this directly and the tag table itself is the cache
(a market already tagged at the current CLASSIFIER_VERSION is never re-sent).

Canonical-name normalisation (e.g. "Los Angeles D" → "Los Angeles Dodgers") is
done by the LLM via world knowledge; this module only validates/truncates the
structured result so one bad field can't corrupt a row or mis-group a game.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine import nfl_reference
from pmi_core.llm import get_provider, render_prompt
from pmi_core.models import CoreMarket

log = structlog.get_logger(__name__)

# Bump when the prompt/grammar changes so a re-tag is detectable (rows with an
# older classifier_version are stale). Mirrors CoreMarketRaceTag.classifier_version.
CLASSIFIER_VERSION = 1

# Sentinel for a candidate the LLM judged NOT a sports market we handle. We
# PERSIST this (instead of dropping it) so the market is never re-sent to the LLM
# — core_market_game_tag doubles as a negative cache. Every sports_compute board
# filters sport=<real sport>, so a "none" row is invisible to all of them; only
# the tag-games candidate query (id NOT IN tagged-at-version) reads it, and it now
# skips these too. Before this, non-game candidates (≈90% of each run) were
# re-classified by the LLM every hour forever (2026-10-27).
NON_GAME_SENTINEL = "none"

# gpt-4o-mini: the platform's classification model since 2026-08-28 (ollama can't
# do reliable structured extraction). `get_provider` routes by the id prefix.
TAG_MODEL_ID = "gpt-4o-mini-2024-07-18"

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/factors/game-tag-extraction-v1.md"
_PROMPT_TEXT = _PROMPT_PATH.read_text(encoding="utf-8")

# A real `FactorSpec` so `provider.evaluate` parses the extraction envelope
# (value 0/1 + canonical-JSON fields). Not registered anywhere — used in-process.
_SPEC = FactorSpec(
    id="game_tag_extraction",
    type="extraction",
    prompt_ref="prompts/factors/game-tag-extraction-v1",
)

_SPORTS = {"mlb", "nfl", "nba", "nhl", "soccer", "tennis", "mma", "golf", "other"}
_CLASSES = {"game", "outright", "prop"}
_TYPES = {
    "moneyline", "runline", "spread", "total",
    "series", "matchup", "mvp", "prop", "other",
}
# Per-column max lengths (keep in lock-step with the model / migration).
_MAX = {"sport": 16, "league": 32, "market_class": 12, "home_team": 64,
        "away_team": 64, "event": 80, "competitor": 80, "market_type": 16, "side": 64}


def _s(fields: dict, key: str) -> str | None:
    """Trimmed string for `key`, truncated to the column width, or None."""
    v = fields.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s[: _MAX[key]] if s else None


def _game_date(fields: dict, market: CoreMarket) -> datetime | None:
    """LLM `game_date` ('YYYY-MM-DD') → aware datetime; fall back to closes_at."""
    raw = fields.get("game_date")
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip()[:10])
            return dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return market.closes_at


# core_market_game_tag.line_value is Numeric(6,2): a value at or beyond ±10,000 is
# an LLM mis-read (a year, a score), and letting it through overflowed the INSERT
# and sank the whole 400-row chunk (tag-games, 2026-10-20). Drop the line, keep the tag.
LINE_ABS_MAX = 10_000.0


def _line(fields: dict) -> float | None:
    try:
        v = fields.get("line_value")
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    if f is None or not math.isfinite(f) or abs(f) >= LINE_ABS_MAX:
        return None
    return f


_KALSHI_NFL_GAME_PREFIXES = ("KXNFLGAME-", "KXNFLSPREAD-")


def _enrich_kalshi_nfl(row: dict, market: CoreMarket) -> None:
    """Fill the opponent + fix the date for a kalshi NFL game from its ticker.

    LLM-on-title leaves away_team empty and guesses game_date (the title names one
    team, no date); the ticker has both deterministically. Keep the LLM's subject
    team as home_team and set away_team to the other side of the pair; if the LLM's
    team isn't in the ticker pair at all, trust the ticker for both. Date always
    comes from the ticker (overrides the LLM guess)."""
    if market.venue != "kalshi" or row["sport"] != "nfl" or row["market_class"] != "game":
        return
    parsed = nfl_reference.parse_game_ticker(market.external_id)
    if parsed is None:
        return
    team_a, team_b, game_date = parsed
    subject = row.get("home_team")
    if subject == team_a:
        row["away_team"] = team_b
    elif subject == team_b:
        row["away_team"] = team_a
    else:  # LLM's team not in the ticker — trust the ticker outright
        row["home_team"], row["away_team"] = team_a, team_b
    row["game_date"] = game_date
    row["resolved_via"] = "ticker+llm"
    # Only KXNFLGAME / KXNFLSPREAD price the full-game winner / spread (and
    # tag-sports-kalshi owns those). The LLM reads e.g. KXNFLWINMARGIN as a
    # moneyline with no side, which the board would fold as P(home wins).
    ext = market.external_id or ""
    if row["market_type"] in ("moneyline", "spread") and not ext.startswith(_KALSHI_NFL_GAME_PREFIXES):
        row["market_type"] = "prop"


def to_tag_row(market: CoreMarket, fields: dict[str, Any]) -> dict | None:
    """Coerce a parsed extraction into a `core_market_game_tag` row, or None if
    the result isn't a usable sports identity (bad class → drop, never guess)."""
    market_class = (_s(fields, "market_class") or "").lower()
    if market_class not in _CLASSES:
        return None
    sport = (_s(fields, "sport") or "other").lower()
    if sport not in _SPORTS:
        sport = "other"
    market_type = (_s(fields, "market_type") or "other").lower()
    if market_type not in _TYPES:
        market_type = "other"
    row = {
        "market_id": market.id,
        "sport": sport,
        "league": _s(fields, "league"),
        "market_class": market_class,
        "home_team": _s(fields, "home_team"),
        "away_team": _s(fields, "away_team"),
        "game_date": _game_date(fields, market),
        "event": _s(fields, "event"),
        "competitor": _s(fields, "competitor"),
        "market_type": market_type,
        "line_value": _line(fields),
        "side": _s(fields, "side"),
        "resolved_via": "llm_fallback",
        "classifier_version": CLASSIFIER_VERSION,
    }
    # Deterministic venue-structured enrichment — recover what LLM-on-title misses.
    # (Gemini + Kalshi have their own deterministic taggers; Kalshi NFL still gets
    # its opponent/date backfilled here when it rode in via the LLM title path.)
    _enrich_kalshi_nfl(row, market)
    return row


def negative_tag_row(market_id: int) -> dict:
    """A negative-cache row: "this market is not a sports game/outright/prop".

    Carries the NOT-NULL columns with NON_GAME_SENTINEL so it satisfies the schema
    while staying invisible to every board (they all filter on a real sport). Its
    only job is to keep the market out of the tag-games candidate set so it is not
    re-sent to the LLM every run.
    """
    return {
        "market_id": market_id,
        "sport": NON_GAME_SENTINEL,
        "league": None,
        "market_class": NON_GAME_SENTINEL,
        "home_team": None,
        "away_team": None,
        "game_date": None,
        "event": None,
        "competitor": None,
        "market_type": NON_GAME_SENTINEL,
        "line_value": None,
        "side": None,
        "resolved_via": NON_GAME_SENTINEL,
        "classifier_version": CLASSIFIER_VERSION,
    }


async def tag_market(market: CoreMarket, *, model_id: str = TAG_MODEL_ID) -> tuple[dict | None, float]:
    """Classify one market → (tag row or None, cost_usd).

    A real game/outright/prop → its tag row. A definitive "not a sports market"
    (value=0) → a `negative_tag_row` sentinel, PERSISTED as a negative cache so the
    market is never re-sent. None → only a malformed/unparseable extraction; the
    caller skips it and it retries next run.
    """
    rendered = render_prompt(_PROMPT_TEXT, market)
    resp = await get_provider(model_id).evaluate(
        rendered_prompt=rendered, factor=_SPEC, temperature=0.0
    )
    # value < 1 is a definitive verdict ("not a sports market") — negative-cache it.
    # This is the common case (the prefilter over-catches), and dropping it was
    # what re-billed ~90% of tag-games every run. A value≥1 with an empty or
    # unparseable extraction is a malformed result, not a verdict → retry (None).
    if resp.value_numeric < 1.0:
        return negative_tag_row(market.id), resp.cost_usd
    if not resp.value_label:
        return None, resp.cost_usd
    import json

    try:
        fields = json.loads(resp.value_label)
    except (ValueError, TypeError):
        return None, resp.cost_usd
    return to_tag_row(market, fields), resp.cost_usd
