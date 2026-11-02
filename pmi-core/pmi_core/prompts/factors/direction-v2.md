# Factor prompt: direction (v2)

Determine whether a YES resolution on this market would indicate **escalation**,
**de-escalation**, or **neither** of *real-world armed conflict between states or
organized armed groups*.

Return JSON:
```json
{ "value": -1 | 0 | 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

- `+1` = YES means armed conflict is more likely / has escalated
  (an invasion, strike, troop deployment, missile/nuclear test, war breaking out)
- `-1` = YES means armed conflict is less likely / has de-escalated
  (a ceasefire, peace deal, troop withdrawal, hostage release)
- `0`  = **neither** — the market is not about the escalation/de-escalation of
  real-world armed conflict, OR its direction is genuinely symmetric/unclear

**Return `0` (this is important — these are NOT war-direction markets):**
- **Diplomatic logistics**: where/when/whether two leaders *meet*, hold a
  summit, or have a call. The *venue or scheduling* of talks carries no armed-
  conflict direction. e.g. "Will Trump and Putin meet next in Australia?",
  "Will there be a Trump–Putin summit in 2026?" → `0`.
  (A concrete signed *agreement* — an actual ceasefire/peace deal — is still
  `-1`; merely meeting/talking is `0`.)
- **Sports / esports / video games**: any match, tournament, or game result,
  even when the title contains war-like words ("strike", "war", "conflict") or a
  country name. e.g. "Counter-Strike: Team A vs Team B", "Valorant: ... War vs
  ...", "Will <team> win the … Series?" → `0`.
- **Elections, economics, crypto, weather, business, legal cases** and anything
  else not about the use or threat of organized armed force → `0`.

If unsure whether a market is even about armed conflict, prefer `0`.

Market title: {market_title}
Market description: {market_description}
