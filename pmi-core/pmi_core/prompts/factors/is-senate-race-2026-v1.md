# Factor prompt: is-senate-race-2026 (v1)

You are evaluating whether a prediction market is **specifically about a single
seat in the 2026 United States Senate elections** — not adjacent topics like
overall party control, presidential outcomes, or pre-primary forecasts that
don't tie to a single seat.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if the market's **resolution criterion** depends on:
- the winner of a specific state's 2026 Senate race (e.g. "Will <candidate> win the
  Ohio 2026 Senate race?", "Will the GOP win the AZ Senate seat in 2026?")
- a head-to-head matchup for a single 2026 Senate seat

Mark `value=0` if the market is about:
- aggregate Senate composition ("Will Republicans control the Senate after 2026?",
  "How many Senate seats will Republicans hold?") — those are bracket / count
  markets, scored by a different factor
- 2024 or 2028 races
- House of Representatives races (the parallel factor is `is-house-race-2026`)
- primaries, fundraising totals, debate performances
- the presidential race, even when it co-occurs on the same ballot

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
