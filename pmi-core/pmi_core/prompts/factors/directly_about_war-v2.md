# Factor prompt: directly_about_war (v2)

You are evaluating whether a prediction market is **directly about an active or imminent armed conflict** — the fighting itself — not adjacent topics like diplomacy, sanctions, defense spending, or political rhetoric.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if the market's **resolution criterion** depends on:
- whether a specific armed conflict starts, escalates, ends, or reaches a milestone
- whether specific kinetic action (strike, invasion, troop movement, ceasefire) occurs by a date

Mark `value=0` if the market is about:
- **diplomacy / talks logistics** — whether, where, or when leaders meet, hold a
  summit, or have a call (e.g. "Will Trump and Putin meet next in <country>?").
  The venue or scheduling of a meeting is diplomacy, NOT the armed conflict —
  even when the leaders involved are parties to a war. (A concrete *signed
  ceasefire/peace agreement* is still `value=1`; merely meeting/talking is `0`.)
- election outcomes, even of wartime candidates
- defense budgets, treaty signings, sanctions
- sports, esports, or video games whose title merely contains war words
  ("strike", "war") or a country name
- generic political stability without specific conflict reference

The presence of a war-associated leader or country name does NOT by itself make
a market directly about armed conflict — judge the resolution criterion.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
