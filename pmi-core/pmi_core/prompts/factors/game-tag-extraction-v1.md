# Factor prompt: game-tag-extraction (v1)

You are extracting the structured **identity** of a sports betting market from a
prediction-market listing, so the platform can collapse the SAME game or title
race across exchanges (Kalshi / Polymarket / Gemini each word it differently).
You are NOT judging who will win — only WHAT this market is about.

Market title: {market_title}
Venue: {market_venue}

Return JSON with this shape:
```json
{
  "value": 0 or 1,
  "sport": "mlb" | "nfl" | "nba" | "nhl" | "soccer" | "tennis" | "mma" | "golf" | "other",
  "league": "<league code, e.g. MLB, NFL, EPL, UCL, NBA; null if unclear>",
  "market_class": "game" | "outright" | "prop",
  "home_team": "<canonical full team name>",
  "away_team": "<canonical full team name>",
  "game_date": "YYYY-MM-DD",
  "event": "<title/series name, e.g. 2026 World Series, NLCS, 2026 World Series MVP>",
  "competitor": "<canonical full team or player name this leg tracks>",
  "market_type": "moneyline" | "runline" | "spread" | "total" | "series" | "matchup" | "mvp" | "prop" | "other",
  "line_value": <number or null>,
  "side": "<home | away | over | under | yes | the competitor name, or null>",
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}
```

## When to set `value`
- `value=1` for any real sports market: a single game, a title/outright race, or
  a player/team prop tied to a sport.
- `value=0` for non-sports markets, league business (coach/manager hiring,
  trades, stadium, CBA), or anything you cannot confidently place. When
  `value=0`, omit all other fields.
- `value=0` for **esports** (CS2 / Counter-Strike / Valorant / League of
  Legends / Dota / Rocket League, etc.) — those are handled by a separate
  vertical. Only traditional sports belong here.

## Which `market_class`
- **game** — one specific match between two teams on a date (head-to-head).
  Fill `home_team`, `away_team`, and `game_date` (the date the game is played; use
  the venue close time if that is all you have). Set `market_type` to the shape:
  `moneyline` (who wins), `runline`/`spread` (handicap, with `line_value` the
  points/runs e.g. -1.5 and `side` the team), `total` (combined over/under, with
  `line_value` the total and `side` = over/under), or `prop` for in-game props.
- **outright** — a race for a title/award decided over a season or series (not a
  single game): "win the World Series", "win the AL pennant", "be the NLCS
  matchup", "World Series MVP". Fill `event` (the title) and `competitor` (the
  team or player this leg backs). `market_type` is `series` (win the title/round),
  `matchup` (two teams named as the matchup), or `mvp`.
- **prop** — a player/team statistical prop not tied to one game's result
  ("most home runs this season", "3,299.5+ passing yards"). Fill `competitor`
  (the player/team) and `event`. Display-only; never folded into odds.

## Canonical names (important — this is the whole point)
Normalise every team/player to its canonical full name using your own knowledge,
so variants across venues collapse to one:
- "Dodgers" / "Los Angeles D" / "LA Dodgers" → **"Los Angeles Dodgers"**
- "Man City" / "Man. City" / "MCFC" → **"Manchester City"**
- "Packers" / "GB" / "Green Bay" → **"Green Bay Packers"**
Do the same for players ("Ohtani" → "Shohei Ohtani").

## Examples
- `"San Diego vs Milwaukee - San Diego"` (gemini)
  → value=1, sport=mlb, league=MLB, market_class=game,
    home_team="San Diego Padres", away_team="Milwaukee Brewers",
    market_type=moneyline, side="San Diego Padres"
  (gemini lists one row per side; `side` is the team this YES backs)
- `"Will the Los Angeles Dodgers win the 2026 World Series?"` (polymarket)
  → value=1, sport=mlb, league=MLB, market_class=outright,
    event="2026 World Series", competitor="Los Angeles Dodgers", market_type=series
- `"Will New York Yankees vs Milwaukee Brewers be the 2026 World Series matchup?"`
  → value=1, sport=mlb, market_class=outright, event="2026 World Series matchup",
    competitor="New York Yankees / Milwaukee Brewers", market_type=matchup
- `"Will Shohei Ohtani win NLCS MVP?"` (kalshi)
  → value=1, sport=mlb, market_class=outright, event="NLCS MVP",
    competitor="Shohei Ohtani", market_type=mvp
- `"Will Drake Maye have 3,299.5+ passing yards in the 2026-27 NFL regular season?"`
  → value=1, sport=nfl, market_class=prop, competitor="Drake Maye",
    event="2026-27 NFL passing yards", market_type=prop, line_value=3299.5, side="over"
- `"Spread: Packers (-2.5)"` (polymarket)
  → value=1, sport=nfl, market_class=game, market_type=spread,
    competitor="Green Bay Packers", line_value=-2.5, side="Green Bay Packers"
- `"Bears vs. Packers: O/U 60.5"` (polymarket)
  → value=1, sport=nfl, market_class=game, home_team="Green Bay Packers",
    away_team="Chicago Bears", market_type=total, line_value=60.5, side="over/under"
- `"Will an NL Central team win the 2026 World Series?"`
  → value=1, sport=mlb, market_class=outright, event="2026 World Series",
    competitor="NL Central (group)", market_type=series
- `"Who will be the next Manager of the Philadelphia Phillies? - Alex Cora"`
  → value=0  (league business, not a bet on a game or title race)
- `"Will it rain in Chicago on Oct 7?"` → value=0  (not sports)

Prefer `confidence < 0.5` and `value=0` over guessing when the sport, teams, or
class are genuinely unclear — a missed tag is recoverable on re-tag; a wrong one
silently mis-groups a game across venues.
