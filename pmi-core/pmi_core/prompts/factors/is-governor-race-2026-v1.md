# Factor prompt: is-governor-race-2026 (v1)

You are evaluating whether a prediction market is **specifically about a single
2026 United States gubernatorial (governor) election** — not adjacent topics
like Senate or House races, party control of governorships nationally, or
pre-primary forecasts that don't tie to a single state's governor race.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if the market's **resolution criterion** depends on:
- the winner of a specific state's 2026 governor race (e.g. "Will the Republicans
  win the Texas gubernatorial race in 2026?", "Will the Democrats hold the
  Pennsylvania governorship in 2026?")
- a head-to-head matchup for a single 2026 governorship

Mark `value=0` if the market is about:
- aggregate counts ("How many governorships will Republicans hold after 2026?")
  — those are bracket / count markets, scored by a different factor
- 2024 / 2025 (NJ, VA) / 2027 (LA, MS, KY) or other off-2026-cycle races
- Senate races (the parallel factor is `is-senate-race-2026`) or House races
  (`is-house-race-2026`)
- state-legislature chambers ("Will Republicans win the Texas House of
  Representatives?"), lieutenant-governor or other down-ballot offices
- primaries, nominations, runoffs, fundraising totals, recall elections
- the presidential race, even when it co-occurs on the same ballot

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
