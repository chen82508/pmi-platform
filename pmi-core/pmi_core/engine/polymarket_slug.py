"""Deterministic parse of Polymarket game slugs — the clean matchup id the LLM
tagger can't read off the title (so it guesses the date, wrong).

Polymarket buries a structured slug in the market's `raw` payload, away-team-first:
  mlb-mil-lad-2026-10-15              game winner (moneyline)
  mlb-tb-cle-2026-10-15-total-7pt5   full-game total (O/U 7.5)
The slug also separates real single games from everything else the LLM mis-tags as
mlb: a non-"mlb-" slug (cpbl-/npb-/cfb-/cs2-…) or a non-game suffix
(-team-total-/-f5-/-nrfi/-inning-/-spread-) is not a full-game market and returns
None. Run lines (-spread-) are skipped — their sign is ambiguous and Kalshi carries
no MLB spread, so they add little. Mirrors nfl/mlb ticker + gemini symbol parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pmi_core.engine import mlb_reference

# Polymarket slug codes are our lowercased abbrevs; add the few that diverge so an
# unseen playoff team still resolves (membership drops anything still unknown).
_CODE = {k.lower(): v for k, v in mlb_reference._ABBREV_TO_TEAM.items()}
_CODE.update({"oak": "Athletics", "was": "Washington Nationals", "az": "Arizona Diamondbacks"})

_DATE = r"(\d{4})-(\d{2})-(\d{2})"
_ML_RE = re.compile(rf"^mlb-([a-z]+)-([a-z]+)-{_DATE}$")
_TOTAL_RE = re.compile(rf"^mlb-([a-z]+)-([a-z]+)-{_DATE}-total-(\d+)pt(\d+)$")


@dataclass(frozen=True)
class PolyGame:
    away: str
    home: str
    game_date: datetime
    market_type: str  # "moneyline" | "total"
    line: float | None
    side: str | None  # moneyline: the team the stored price is P(win) for (= away)


def _game(a: str, b: str, y: str, mo: str, d: str) -> tuple[str, str, datetime] | None:
    away, home = _CODE.get(a), _CODE.get(b)
    if not away or not home:
        return None
    try:
        return away, home, datetime(int(y), int(mo), int(d), tzinfo=UTC)
    except ValueError:
        return None


def parse_mlb_game(slug: str | None) -> PolyGame | None:
    """Polymarket slug → a full-game moneyline/total, or None if it isn't one."""
    if not slug:
        return None
    m = _ML_RE.match(slug)
    if m:
        g = _game(*m.groups())
        # Polymarket lists the matchup away-first and prices outcomes[0] (= away),
        # so the stored last_price is P(away wins).
        return PolyGame(*g, "moneyline", None, side=g[0]) if g else None
    m = _TOTAL_RE.match(slug)
    if m:
        a, b, y, mo, d, whole, frac = m.groups()
        g = _game(a, b, y, mo, d)
        return PolyGame(*g, "total", float(f"{whole}.{frac}"), side=None) if g else None
    return None
