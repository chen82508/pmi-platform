# Factor prompt: is-house-race-2026 (v1)

You are evaluating whether a prediction market is **specifically about a single
seat in the 2026 United States House of Representatives elections** — not
adjacent topics like overall party control, Senate races, or pre-primary
forecasts that don't tie to a single seat.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if the market's **resolution criterion** depends on:
- the winner of a specific congressional district's 2026 House race
  (e.g. "Will <candidate> win NY-17 in 2026?", "Will the GOP hold CA-22 in 2026?")
- a head-to-head matchup for a single 2026 House seat

Mark `value=0` if the market is about:
- aggregate House composition ("Will Republicans control the House after 2026?",
  "How many House seats will Republicans hold?") — those are bracket / count
  markets, scored by a different factor
- 2024 or 2028 races
- Senate races (the parallel factor is `is-senate-race-2026`)
- primaries, fundraising totals, redistricting outcomes that aren't seat results
- the presidential race, even when it co-occurs on the same ballot

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
