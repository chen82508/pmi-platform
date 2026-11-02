"""MLB reference data — stable team metadata for the Sports vertical.

The bracket itself is derived LIVE from the market feed (current-round matchups
only; it advances as rounds resolve and never fabricates past rounds). The one
thing the feed can't give cleanly is a short, fan-facing label — team names come
through as full canonical strings that truncate in a bracket card. So this module
maps each canonical name to its nickname + abbrev + league + division, and the
bracket renders "Yankees" instead of "New York Yankees".

(A hand-maintained POSTSEASON_2026 bracket skeleton was considered to show the
full 12-team field with eliminated teams dimmed, but that means hardcoding Wild
Card results the feed can't confirm — rejected to keep the vertical honest.)
"""

from __future__ import annotations

from datetime import datetime

from pmi_core.engine import sports_ticker

# canonical name (matches core_market_game_tag.competitor / the _MLB_TEAMS
# whitelist) -> short nickname + abbrev + league + division.
TEAMS: dict[str, dict[str, str]] = {
    # AL East
    "Baltimore Orioles": {"short": "Orioles", "abbrev": "BAL", "league": "AL", "division": "East"},
    "Boston Red Sox": {"short": "Red Sox", "abbrev": "BOS", "league": "AL", "division": "East"},
    "New York Yankees": {"short": "Yankees", "abbrev": "NYY", "league": "AL", "division": "East"},
    "Tampa Bay Rays": {"short": "Rays", "abbrev": "TB", "league": "AL", "division": "East"},
    "Toronto Blue Jays": {"short": "Blue Jays", "abbrev": "TOR", "league": "AL", "division": "East"},
    # AL Central
    "Chicago White Sox": {"short": "White Sox", "abbrev": "CWS", "league": "AL", "division": "Central"},
    "Cleveland Guardians": {"short": "Guardians", "abbrev": "CLE", "league": "AL", "division": "Central"},
    "Detroit Tigers": {"short": "Tigers", "abbrev": "DET", "league": "AL", "division": "Central"},
    "Kansas City Royals": {"short": "Royals", "abbrev": "KC", "league": "AL", "division": "Central"},
    "Minnesota Twins": {"short": "Twins", "abbrev": "MIN", "league": "AL", "division": "Central"},
    # AL West
    "Houston Astros": {"short": "Astros", "abbrev": "HOU", "league": "AL", "division": "West"},
    "Los Angeles Angels": {"short": "Angels", "abbrev": "LAA", "league": "AL", "division": "West"},
    "Athletics": {"short": "Athletics", "abbrev": "ATH", "league": "AL", "division": "West"},
    "Seattle Mariners": {"short": "Mariners", "abbrev": "SEA", "league": "AL", "division": "West"},
    "Texas Rangers": {"short": "Rangers", "abbrev": "TEX", "league": "AL", "division": "West"},
    # NL East
    "Atlanta Braves": {"short": "Braves", "abbrev": "ATL", "league": "NL", "division": "East"},
    "Miami Marlins": {"short": "Marlins", "abbrev": "MIA", "league": "NL", "division": "East"},
    "New York Mets": {"short": "Mets", "abbrev": "NYM", "league": "NL", "division": "East"},
    "Philadelphia Phillies": {"short": "Phillies", "abbrev": "PHI", "league": "NL", "division": "East"},
    "Washington Nationals": {"short": "Nationals", "abbrev": "WSH", "league": "NL", "division": "East"},
    # NL Central
    "Chicago Cubs": {"short": "Cubs", "abbrev": "CHC", "league": "NL", "division": "Central"},
    "Cincinnati Reds": {"short": "Reds", "abbrev": "CIN", "league": "NL", "division": "Central"},
    "Milwaukee Brewers": {"short": "Brewers", "abbrev": "MIL", "league": "NL", "division": "Central"},
    "Pittsburgh Pirates": {"short": "Pirates", "abbrev": "PIT", "league": "NL", "division": "Central"},
    "St. Louis Cardinals": {"short": "Cardinals", "abbrev": "STL", "league": "NL", "division": "Central"},
    # NL West
    "Arizona Diamondbacks": {"short": "D-backs", "abbrev": "ARI", "league": "NL", "division": "West"},
    "Colorado Rockies": {"short": "Rockies", "abbrev": "COL", "league": "NL", "division": "West"},
    "Los Angeles Dodgers": {"short": "Dodgers", "abbrev": "LAD", "league": "NL", "division": "West"},
    "San Diego Padres": {"short": "Padres", "abbrev": "SD", "league": "NL", "division": "West"},
    "San Francisco Giants": {"short": "Giants", "abbrev": "SF", "league": "NL", "division": "West"},
}


def short_name(canonical: str) -> str:
    """Fan-facing nickname for a canonical team name; falls back to the input."""
    meta = TEAMS.get(canonical)
    return meta["short"] if meta else canonical


def abbrev(canonical: str) -> str:
    meta = TEAMS.get(canonical)
    return meta["abbrev"] if meta else canonical[:3].upper()


# Reverse map for parsing exchange ticker / symbol codes back to a team (mirrors
# nfl_reference). Gemini's code for the White Sox is CHW, not the CWS we store —
# the only divergence across the live MLB slate (audited 2026-10-14: 30 distinct
# Gemini MLB game codes, CHW the only one unknown). Alias it; `abbrev()` still
# emits CWS for display.
_ABBREV_TO_TEAM = {m["abbrev"]: name for name, m in TEAMS.items()}
_ABBREV_TO_TEAM["CHW"] = "Chicago White Sox"
# Kalshi's code for Arizona is AZ (KXMLB-26-AZ), not ARI.
_ABBREV_TO_TEAM["AZ"] = "Arizona Diamondbacks"


# Kalshi KXMLBGAME tickers carry the matchup + first-pitch time
# (KXMLBGAME-26OCT152000MILLAD-MIL); the shared parser reads them against this
# sport's abbrev map. with_time=True — MLB tickers carry the HHMM block.
def parse_game_ticker(ticker: str | None) -> tuple[str, str, datetime] | None:
    """Kalshi KXMLBGAME ticker → (away, home, game_date), or None. Away-first."""
    return sports_ticker.parse_game_ticker(ticker, _ABBREV_TO_TEAM, with_time=True)
