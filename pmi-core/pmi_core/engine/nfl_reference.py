"""NFL reference data — team metadata + the whitelist that separates NFL from NCAA.

The LLM tags college football (NCAA) under sport='nfl' too, so the single most
important thing here is TEAMS: the 32-team whitelist. A game is only a real NFL
game when both sides are in it — that one check drops the Arkansas State / Sam
Houston / Duke / Ole Miss college markets that otherwise flood the feed.

short_name() gives the fan nickname the game cards render ("Chiefs", not "Kansas
City Chiefs"); abbrev() the 2-3 letter code. The bracket/odds themselves are
always derived live from the market feed, never hardcoded here.
"""

from __future__ import annotations

from datetime import datetime

from pmi_core.engine import sports_ticker

# canonical name -> nickname + abbrev + conference + division.
TEAMS: dict[str, dict[str, str]] = {
    # AFC East
    "Buffalo Bills": {"short": "Bills", "abbrev": "BUF", "conf": "AFC", "division": "East"},
    "Miami Dolphins": {"short": "Dolphins", "abbrev": "MIA", "conf": "AFC", "division": "East"},
    "New England Patriots": {"short": "Patriots", "abbrev": "NE", "conf": "AFC", "division": "East"},
    "New York Jets": {"short": "Jets", "abbrev": "NYJ", "conf": "AFC", "division": "East"},
    # AFC North
    "Baltimore Ravens": {"short": "Ravens", "abbrev": "BAL", "conf": "AFC", "division": "North"},
    "Cincinnati Bengals": {"short": "Bengals", "abbrev": "CIN", "conf": "AFC", "division": "North"},
    "Cleveland Browns": {"short": "Browns", "abbrev": "CLE", "conf": "AFC", "division": "North"},
    "Pittsburgh Steelers": {"short": "Steelers", "abbrev": "PIT", "conf": "AFC", "division": "North"},
    # AFC South
    "Houston Texans": {"short": "Texans", "abbrev": "HOU", "conf": "AFC", "division": "South"},
    "Indianapolis Colts": {"short": "Colts", "abbrev": "IND", "conf": "AFC", "division": "South"},
    "Jacksonville Jaguars": {"short": "Jaguars", "abbrev": "JAX", "conf": "AFC", "division": "South"},
    "Tennessee Titans": {"short": "Titans", "abbrev": "TEN", "conf": "AFC", "division": "South"},
    # AFC West
    "Denver Broncos": {"short": "Broncos", "abbrev": "DEN", "conf": "AFC", "division": "West"},
    "Kansas City Chiefs": {"short": "Chiefs", "abbrev": "KC", "conf": "AFC", "division": "West"},
    "Las Vegas Raiders": {"short": "Raiders", "abbrev": "LV", "conf": "AFC", "division": "West"},
    "Los Angeles Chargers": {"short": "Chargers", "abbrev": "LAC", "conf": "AFC", "division": "West"},
    # NFC East
    "Dallas Cowboys": {"short": "Cowboys", "abbrev": "DAL", "conf": "NFC", "division": "East"},
    "New York Giants": {"short": "Giants", "abbrev": "NYG", "conf": "NFC", "division": "East"},
    "Philadelphia Eagles": {"short": "Eagles", "abbrev": "PHI", "conf": "NFC", "division": "East"},
    "Washington Commanders": {"short": "Commanders", "abbrev": "WAS", "conf": "NFC", "division": "East"},
    # NFC North
    "Chicago Bears": {"short": "Bears", "abbrev": "CHI", "conf": "NFC", "division": "North"},
    "Detroit Lions": {"short": "Lions", "abbrev": "DET", "conf": "NFC", "division": "North"},
    "Green Bay Packers": {"short": "Packers", "abbrev": "GB", "conf": "NFC", "division": "North"},
    "Minnesota Vikings": {"short": "Vikings", "abbrev": "MIN", "conf": "NFC", "division": "North"},
    # NFC South
    "Atlanta Falcons": {"short": "Falcons", "abbrev": "ATL", "conf": "NFC", "division": "South"},
    "Carolina Panthers": {"short": "Panthers", "abbrev": "CAR", "conf": "NFC", "division": "South"},
    "New Orleans Saints": {"short": "Saints", "abbrev": "NO", "conf": "NFC", "division": "South"},
    "Tampa Bay Buccaneers": {"short": "Buccaneers", "abbrev": "TB", "conf": "NFC", "division": "South"},
    # NFC West
    "Arizona Cardinals": {"short": "Cardinals", "abbrev": "ARI", "conf": "NFC", "division": "West"},
    "Los Angeles Rams": {"short": "Rams", "abbrev": "LAR", "conf": "NFC", "division": "West"},
    "San Francisco 49ers": {"short": "49ers", "abbrev": "SF", "conf": "NFC", "division": "West"},
    "Seattle Seahawks": {"short": "Seahawks", "abbrev": "SEA", "conf": "NFC", "division": "West"},
}

TEAM_NAMES = frozenset(TEAMS)


def short_name(canonical: str) -> str:
    meta = TEAMS.get(canonical)
    return meta["short"] if meta else canonical


def abbrev(canonical: str) -> str:
    meta = TEAMS.get(canonical)
    return meta["abbrev"] if meta else canonical[:3].upper()


# ─────────────────────────────────────────────────────────────────────────────
# Kalshi ticker parsing — the opponent the LLM misses.
#
# Kalshi NFL markets are single-team by title ("ARI Cardinals wins by over 1.5?")
# so LLM-on-title fills home_team but leaves away_team empty and GUESSES the date.
# Both the matchup AND the date are in the ticker, deterministically:
#   KXNFL1HSPREAD-26OCT11DETARI-ARI10
#                 └YY MMM DD┘└teams┘
# so a regex + the abbrev map recover them with no LLM. (Same pattern the election
# path uses to read kalshi tickers in election_cycle.py.)
# ─────────────────────────────────────────────────────────────────────────────
_ABBREV_TO_TEAM = {m["abbrev"]: name for name, m in TEAMS.items()}
# Kalshi's ticker code for Jacksonville is JAC, not the JAX we use for display —
# the only divergence across the live NFL slate (audited 2026-10-13: of 415
# KXNFLGAME/SPREAD markets, the 27 that failed to parse were all HOUJAC). Alias it
# so the ticker parser resolves it; `abbrev()` still emits JAX for display/slugs.
_ABBREV_TO_TEAM["JAC"] = "Jacksonville Jaguars"
# Kalshi KXNFLGAME / KXNFLSPREAD tickers carry the matchup + date, no game time
# (KXNFLGAME-26OCT12BUFLAR); the shared parser reads them against this sport's
# abbrev map. with_time=False — NFL tickers have no HHMM block.
def parse_game_ticker(ticker: str | None) -> tuple[str, str, datetime] | None:
    """Kalshi NFL ticker → (team_a, team_b, game_date), or None. Ticker order."""
    return sports_ticker.parse_game_ticker(ticker, _ABBREV_TO_TEAM, with_time=False)
