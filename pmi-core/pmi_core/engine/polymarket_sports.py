"""Deterministic game tag for Polymarket sports markets — no LLM.

Every Polymarket sports market carries structured fields in `raw`:
  slug              cfb-uk-tenn-2026-11-07-spread-home-12pt5   (league-away-home-date-…)
  sportsMarketType  moneyline | spreads | totals | q1_totals | rushing_yards | …
  line              -12.5
  gameStartTime     2026-11-07 23:15:00+00
so `tag-games` reads the tag off them instead of asking gpt-4o-mini. Polymarket
lists every alternate line as its own market (one CFB game ≈ 60 markets), which made
these ~90% of tag-games' LLM spend (2026-10-27) — and the LLM got them wrong anyway:
college football tagged `nfl` with NFL team names ("Tennessee" → Titans), so a
CFB spread could attach to an NFL matchup, and dates guessed as 2023.

The leagues a board reads (mlb, nfl) are tagged only when both slug team codes
resolve against the reference rosters; otherwise None → the market falls back to
the LLM, so an unseen code never silently drops a board market. Every other league
is tagged with sport from the slug prefix ("other" when unmapped) — no board reads
them, the row only has to keep the market out of the LLM candidate set.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from pmi_core.engine import nfl_reference, polymarket_slug
from pmi_core.engine.polymarket_slug import parse_mlb_game
from pmi_core.engine.sports_tag import CLASSIFIER_VERSION, negative_tag_row

_SPORT_BY_PREFIX = {
    "mlb": "mlb", "nfl": "nfl", "nba": "nba", "nhl": "nhl",
    "atp": "tennis", "wta": "tennis", "itf": "tennis",
    "ufc": "mma", "pga": "golf", "lpga": "golf",
}
# Esports is its own vertical; mirror the LLM's "not a game we handle" verdict.
_ESPORTS_PREFIXES = {"cs2", "lol", "val", "valorant", "dota2", "r6", "rl", "cod", "ow"}
# Full-game headline markets; everything else (periods, team totals, player
# props, handicaps) is a prop, which no game board folds.
_GAME_TYPES = {"moneyline", "spreads", "totals"}

_TEAMS = {
    # Polymarket slugs the Rams as "la" (nfl-lac-la-…), not LAR. Unresolved codes
    # fall back to the LLM, which tagged the Rams' team totals and TD/yardage props
    # as game totals (audited 2026-10-29: la was the only unmatched NFL code).
    "nfl": {k.lower(): v for k, v in nfl_reference._ABBREV_TO_TEAM.items()} | {"la": "Los Angeles Rams"},
    "mlb": polymarket_slug._CODE,  # already lowercased, with Polymarket's aliases
}

_SLUG_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4})-(\d{2})-(\d{2})(?:-(.+))?$")
_SPREAD_SIDE_RE = re.compile(r"(?:^|-)spread-(home|away)-")
# Same bound as sports_tag._line: core_market_game_tag.line_value is Numeric(6,2).
_LINE_ABS_MAX = 10_000.0


def _market_type(smt: str) -> str:
    if smt == "moneyline":
        return "moneyline"
    if "spread" in smt or "handicap" in smt:
        return "spread"
    if "total" in smt:
        return "total"
    return "prop"


def _line(raw: dict) -> float | None:
    try:
        f = float(raw["line"])
    except (KeyError, TypeError, ValueError):
        return None
    return f if math.isfinite(f) and abs(f) < _LINE_ABS_MAX else None


def _start(raw: dict, slug_date: tuple[str, str, str]) -> datetime | None:
    s = raw.get("gameStartTime")
    if isinstance(s, str) and s.strip():
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime(*(int(p) for p in slug_date), tzinfo=UTC)
    except ValueError:
        return None


def _outcome0(raw: dict) -> str | None:
    """outcomes[0] — the outcome the stored last_price is P() of."""
    o = raw.get("outcomes")
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except ValueError:
            return None
    return str(o[0]) if isinstance(o, list) and o else None


def _matchup(title: str | None) -> str | None:
    """"UCLA vs. Minnesota: 1Q O/U 7.5" → "UCLA vs. Minnesota" (kept as event)."""
    head = (title or "").split(":", 1)[0].strip()
    return head[:80] if " vs" in head else None


def _nfl_side(mtype: str, slug_rest: str, away: str, home: str, raw: dict) -> str:
    """The outcome the stored price is P() of, as the NFL board reads `side`."""
    if mtype == "total":
        return "over"  # outcomes are ["Over", "Under"]
    if mtype == "spread":
        m = _SPREAD_SIDE_RE.search(slug_rest)
        if m:
            return home if m.group(1) == "home" else away
    # moneyline (or a spread without a home/away token): the team outcomes[0] names.
    o0 = (_outcome0(raw) or "").lower()
    for team in (away, home):
        if o0 and o0 == nfl_reference.short_name(team).lower():
            return team
    return away  # Polymarket lists the matchup away-first


def parse_polymarket_sports(market: Any) -> dict | None:
    """A `core_market_game_tag` row from the market's structured Polymarket fields
    (a negative-cache row for esports), or None when it must go to the LLM: no
    sportsMarketType, unparseable slug, or a board league whose codes don't resolve."""
    if getattr(market, "venue", None) != "polymarket":
        return None
    raw = market.raw if isinstance(market.raw, dict) else None
    smt = (raw or {}).get("sportsMarketType")
    if not isinstance(smt, str) or not smt:
        return None
    m = _SLUG_RE.match(str(raw.get("slug") or ""))
    if not m:
        return None
    prefix, a, b, y, mo, d, rest = m.groups()
    rest = rest or ""
    if prefix in _ESPORTS_PREFIXES:
        return negative_tag_row(market.id)

    sport = _SPORT_BY_PREFIX.get(prefix, "other")
    mtype = _market_type(smt)
    row: dict[str, Any] = {
        "market_id": market.id,
        "sport": sport,
        "league": prefix[:32],
        "market_class": "game" if smt in _GAME_TYPES else "prop",
        "home_team": None,
        "away_team": None,
        "game_date": _start(raw, (y, mo, d)),
        "event": _matchup(market.title),
        "competitor": None,
        "market_type": mtype,
        "line_value": _line(raw),
        "side": None,
        "resolved_via": "slug",
        "classifier_version": CLASSIFIER_VERSION,
    }

    codes = _TEAMS.get(sport)
    if codes is None:
        return row
    away, home = codes.get(a), codes.get(b)
    if not away or not home:
        return None  # board league, unknown code → LLM
    row.update(away_team=away, home_team=home)
    if sport == "mlb" and row["market_class"] == "game":
        # Reuse the MLB slug parser so its date (slug date, the local game day the
        # MLB board buckets on) and side match what tag-sports-polymarket writes.
        g = parse_mlb_game(raw.get("slug"))
        if g is None:
            return None  # run lines etc. — leave to the LLM, as before
        row.update(game_date=g.game_date, line_value=g.line, side=g.side)
        return row
    if sport == "nfl" and row["market_class"] == "game":
        row["side"] = _nfl_side(mtype, rest, away, home, raw)
    return row
