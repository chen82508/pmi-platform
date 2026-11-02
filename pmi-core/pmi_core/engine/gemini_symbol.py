"""gemini_symbol — parse Gemini's instrumentSymbol into a canonical game identity.

Gemini labels every market by city only ("Detroit vs Minnesota"), so LLM-on-title
guesses the sport AND the teams — and gets both wrong (a Guardians/Rays MLB game
tagged "Cleveland Browns vs Tampa Bay Buccaneers"). The instrumentSymbol carries
all of it deterministically:

    GEMI-<SPORT>-<YYMMDDHHMM>-<AWAY>-<HOME>-<TYPE>-<SIDE>
    e.g. GEMI-MLB-2606302235-CHW-BAL-M-BAL  (Chicago White Sox at Baltimore)

Sports we render (NFL/MLB) resolve their team codes via that sport's reference —
whose abbrev map already carries the exchange-code aliases (JAC->Jaguars,
CHW->White Sox). Every other sport collapses to "other" so it drops off the
boards. Adding a sport = one entry in ``_SPORT_REF`` (its reference + slug);
nothing else changes. This is the per-venue deterministic counterpart to
nfl_reference.parse_game_ticker (Kalshi) — Gemini never touches the LLM pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pmi_core.engine import mlb_reference, nfl_reference

# Gemini sport token -> (our sport slug, abbrev->canonical-name map). Extend here
# when a vertical gains a reference; the map already carries that sport's aliases.
_SPORT_REF: dict[str, tuple[str, dict[str, str]]] = {
    "NFL": ("nfl", nfl_reference._ABBREV_TO_TEAM),
    "MLB": ("mlb", mlb_reference._ABBREV_TO_TEAM),
}

# A game market: GEMI-<SPORT>-<10-digit stamp>-<AWAY>-<HOME>-<TYPE>-<SIDE>.
_GAME_RE = re.compile(r"^GEMI-([A-Z0-9]+)-(\d{10})-([A-Z]{2,4})-([A-Z]{2,4})-[A-Z]+-")
_SPORT_RE = re.compile(r"^GEMI-([A-Z0-9]+)-")


@dataclass(frozen=True)
class GeminiGame:
    sport: str  # our slug: nfl / mlb / other
    away: str | None  # canonical team name — set only for a served-sport game
    home: str | None
    game_date: datetime | None


def _stamp(s: str) -> datetime | None:
    try:
        return datetime(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]),
                        int(s[6:8]), int(s[8:10]), tzinfo=UTC)
    except ValueError:
        return None


def served_sport_tokens() -> list[str]:
    """Gemini sport tokens we render (NFL, MLB, …) — the tagger's candidate filter."""
    return list(_SPORT_REF)


def parse(symbol: str | None) -> GeminiGame | None:
    """A GeminiGame (sport always set; away/home/date only for a served-sport game),
    or None when the string isn't a Gemini symbol at all."""
    if not isinstance(symbol, str) or not symbol.startswith("GEMI-"):
        return None
    g = _GAME_RE.match(symbol)
    if g:
        token, stamp, away_code, home_code = g.group(1), g.group(2), g.group(3), g.group(4)
        slug, amap = _SPORT_REF.get(token, ("other", {}))
        if slug == "other":
            return GeminiGame("other", None, None, None)
        return GeminiGame(slug, amap.get(away_code), amap.get(home_code), _stamp(stamp))
    # Not a game pattern (a futures/outright symbol): the sport is still in the token.
    m = _SPORT_RE.match(symbol)
    slug = _SPORT_REF.get(m.group(1), ("other", {}))[0] if m else "other"
    return GeminiGame(slug, None, None, None)
