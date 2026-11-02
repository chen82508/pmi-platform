# Factor prompt: directly_about_war (v1)

You are evaluating whether a prediction market is **directly about an active or imminent armed conflict**, not adjacent topics like sanctions, defense spending, or political rhetoric.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if the market's **resolution criterion** depends on:
- whether a specific armed conflict starts, escalates, ends, or reaches a milestone
- whether specific kinetic action (strike, invasion, ceasefire) occurs by a date

Mark `value=0` if the market is about:
- election outcomes, even of wartime candidates
- defense budgets, treaty signings, sanctions
- generic political stability without specific conflict reference

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
