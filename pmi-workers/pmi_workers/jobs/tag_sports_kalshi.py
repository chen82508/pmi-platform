"""tag-sports-kalshi — deterministic game tagging for Kalshi, no LLM.

Kalshi's game markets title only ONE side ("ARI Cardinals wins by over 1.5?",
"Milwaukee wins"), so the title-reading LLM (`tag-games`) can't pair the matchup:
for NFL it fills home_team but leaves away_team empty and guesses the date, and
for the bare "<Team> wins" MLB moneylines it doesn't even recognise them as sports
markets, so those high-volume single-game bets go untagged entirely. But Kalshi's
ticker encodes the matchup AND the date deterministically, away-team-first:

  NFL:  KXNFLGAME-26OCT12BUFLAR          winner   / KXNFLSPREAD-… spread
  MLB:  KXMLBGAME-26OCT152000MILLAD-MIL  winner

so this job recovers them with a regex — free, exact, complete. It finds
candidates via the title trigram index (fast; external_id LIKE has no index) and
dispatches each row to the right sport/market by its ticker prefix. The other
venues (whose titles name both teams) stay on `tag-games`. Cheap enough to run
every few minutes so new slates appear without waiting on an LLM pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import mlb_reference, nfl_reference
from pmi_core.engine.sports_tag import CLASSIFIER_VERSION
from sqlalchemy import text

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.tag_sports_kalshi")

_LINE_RE = re.compile(r"wins by over (\d+(?:\.\d+)?)", re.IGNORECASE)
_TOTAL_RE = re.compile(r"over (\d+(?:\.\d+)?) points", re.IGNORECASE)


@dataclass(frozen=True)
class _Source:
    """One ticker family → how to tag it. `parse` returns (away, home, date)."""
    prefix: str
    sport: str
    market_type: str
    parse: Callable[[str | None], tuple[str, str, datetime] | None]
    abbrev_to_team: dict[str, str]  # trailing "-SIDE" code → canonical team


# Dispatch by ticker prefix. The full-game markets that build a card: the winner
# (moneyline) and, NFL-only, the point spread and game total. Kalshi MLB has no
# spread ticker in the live slate, so MLB is moneyline-only here.
_SOURCES = [
    _Source("KXNFLGAME-", "nfl", "moneyline", nfl_reference.parse_game_ticker, nfl_reference._ABBREV_TO_TEAM),
    _Source("KXNFLSPREAD-", "nfl", "spread", nfl_reference.parse_game_ticker, nfl_reference._ABBREV_TO_TEAM),
    _Source("KXNFLTOTAL-", "nfl", "total", nfl_reference.parse_game_ticker, nfl_reference._ABBREV_TO_TEAM),
    _Source("KXMLBGAME-", "mlb", "moneyline", mlb_reference.parse_game_ticker, mlb_reference._ABBREV_TO_TEAM),
]

# Trigram-indexed probes: winner titles end "<Team> wins", NFL spreads are "<Team>
# wins by over X", and game totals are "Full Game: over X points scored?" (no team
# at all, so tag-games' prefilter never sees them). The prefix dispatch routes.
_TITLE_PROBES = ["%wins%", "full game: over%"]

_UPSERT = text("""
  INSERT INTO core_market_game_tag
    (market_id, sport, market_class, market_type, home_team, away_team, game_date,
     line_value, side, resolved_via, classifier_version, tagged_at)
  VALUES (:mid,:sport,'game',:mt,:home,:away,:date,:line,:side,'ticker',:ver, now())
  ON CONFLICT (market_id) DO UPDATE SET
    sport=:sport, market_class='game', market_type=:mt, home_team=:home,
    away_team=:away, game_date=:date, line_value=:line, side=:side,
    resolved_via='ticker', classifier_version=:ver, tagged_at=now()
""")


def _side(ext: str, abbrev_to_team: dict[str, str]) -> str | None:
    """The team a single-team quote is about — the ticker's trailing code."""
    m = re.match(r"[A-Z]+", ext.rsplit("-", 1)[-1])
    return abbrev_to_team.get(m.group(0)) if m else None


def _tag_row(ext: str | None, title: str | None) -> dict | None:
    """Upsert params (minus id/version) for one Kalshi market, or None if no
    source owns its ticker or the ticker doesn't parse."""
    if not ext:
        return None
    src = next((s for s in _SOURCES if ext.startswith(s.prefix)), None)
    if src is None:
        return None
    parsed = src.parse(ext)
    if parsed is None:
        return None
    away, home, game_date = parsed  # kalshi orders the matchup away-first
    line, side = None, _side(ext, src.abbrev_to_team) or away
    if src.market_type == "spread":
        lm = _LINE_RE.search(title or "")
        # "SUBJECT wins by over X" == SUBJECT favored by X → store −X.
        line = -float(lm.group(1)) if lm else None
    elif src.market_type == "total":
        lm = _TOTAL_RE.search(title or "")
        line, side = (float(lm.group(1)) if lm else None), "over"
    return {"sport": src.sport, "mt": src.market_type, "home": home, "away": away,
            "date": game_date, "line": line, "side": side}


@register("tag-sports-kalshi", queue="batch")
async def run() -> dict:
    tagged: dict[str, int] = {}
    async with session_scope() as session:
        rows = []
        for probe in _TITLE_PROBES:  # one plain ILIKE each, so each uses the trigram index
            rows += (
                await session.execute(
                    text(
                        "SELECT id, external_id, title FROM core_markets "
                        "WHERE venue='kalshi' AND resolved_at IS NULL AND title ILIKE :q"
                    ),
                    {"q": probe},
                )
            ).all()
        for mid, ext, title in rows:
            row = _tag_row(ext, title)
            if row is None:
                continue
            await session.execute(_UPSERT, {"mid": mid, **row, "ver": CLASSIFIER_VERSION})
            key = f"{row['sport']}:{row['mt']}"
            tagged[key] = tagged.get(key, 0) + 1
        await session.commit()

    log.info("tag_sports_kalshi.done", found=len(rows), tagged=tagged)
    return {"found": len(rows), "tagged": tagged}
