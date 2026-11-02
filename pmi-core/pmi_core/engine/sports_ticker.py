"""Kalshi game-ticker parsing, shared across sports.

Kalshi's single-game markets title only one side ("Milwaukee wins", "ARI Cardinals
wins by over 1.5?"), which the title-reading LLM can't pair into a matchup. The
ticker carries the whole matchup + date, concatenated, matchup away-team-first:

  NFL:  KXNFLGAME-26OCT12BUFLAR          └YY MMM DD┘└teams┘
  MLB:  KXMLBGAME-26OCT152000MILLAD-MIL  └YY MMM DD HHMM┘└teams┘└side┘

The grammar is identical except MLB carries a first-pitch HHMM block (NFL doesn't),
so `parse_game_ticker` takes a `with_time` flag. Each sport owns only its team
abbrev→name map (and its code aliases, e.g. Kalshi's JAC/CHW); the parsing lives
here once. Adding a Kalshi sport needs a TEAMS table, not another copy of this.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_MONTHS = {mon: i + 1 for i, mon in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
# -<YY><MMM><DD>[<HHMM>]<4-6 team-code letters>, bounded by the side dash or end.
_RE_NO_TIME = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,6})(?:-|$)")
_RE_WITH_TIME = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})\d{4}([A-Z]{4,6})(?:-|$)")


def split_codes(blob: str, abbrev_to_team: dict[str, str]) -> tuple[str, str] | None:
    """Split a concatenated pair of team codes (e.g. 'MILLAD', 'DETARI') using the
    abbrev set — codes are 2 or 3 chars, so try the valid partition (3 before 2)."""
    for i in (3, 2):
        a, b = blob[:i], blob[i:]
        if a in abbrev_to_team and b in abbrev_to_team:
            return a, b
    return None


def parse_game_ticker(
    ticker: str | None, abbrev_to_team: dict[str, str], *, with_time: bool
) -> tuple[str, str, datetime] | None:
    """Kalshi game ticker → (away, home, game_date) canonical, or None if it isn't a
    recognisable game ticker for this sport. Kalshi orders the matchup away-first.
    `with_time=True` for sports whose ticker carries a HHMM first-pitch block (MLB)."""
    if not ticker:
        return None
    m = (_RE_WITH_TIME if with_time else _RE_NO_TIME).search(ticker)
    if not m:
        return None
    yy, mon, dd, blob = m.groups()
    month = _MONTHS.get(mon)
    pair = split_codes(blob, abbrev_to_team)
    if month is None or pair is None:
        return None
    try:
        date = datetime(2000 + int(yy), month, int(dd), tzinfo=UTC)
    except ValueError:
        return None
    return abbrev_to_team[pair[0]], abbrev_to_team[pair[1]], date
