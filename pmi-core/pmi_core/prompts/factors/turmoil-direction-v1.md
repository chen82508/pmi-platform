# Factor prompt: turmoil-direction (v1)

Determine whether a YES resolution on this market would indicate **escalation**
(more turmoil), **de-escalation** (less turmoil), or **neither**. This is the
direction factor for the Global Instability ("War") Index, which measures a
country's overall level of turmoil: armed conflict, civil unrest, state security
crises, coups, terrorism, political crisis, and severe economic crisis.

Return JSON:
```json
{ "value": -1 | 0 | 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

- `+1` = YES means turmoil is MORE likely / has escalated. Armed conflict:
  an invasion, strike, troop deployment, missile/nuclear test, war breaking out.
  Domestic turmoil: protests/riots/unrest erupt; a state of emergency, state of
  siege, or martial law is DECLARED; a coup succeeds or is attempted; a terrorist
  attack occurs; a government collapses or loses a confidence vote; a leader is
  ousted, forced to resign, or leaves office early; hyperinflation / currency
  collapse / sovereign default / financial collapse occurs.

- `-1` = YES means turmoil is LESS likely / has de-escalated. Armed conflict:
  a ceasefire, peace deal, peace agreement, truce, armistice, troop withdrawal,
  or hostage release. Domestic turmoil: a state of emergency / martial law is
  LIFTED; an embattled government or leader SURVIVES (wins the confidence vote,
  stays in office through the crisis); order is restored; a bailout stabilizes a
  collapsing economy.
  **A ceasefire / peace / truce / withdrawal / crisis-resolution market is `-1`,
  NEVER `0` — YES on it means less armed conflict or less turmoil.**

- `0`  = **neither** — YES does not move a country's turmoil level in either
  direction, OR the direction is genuinely symmetric/unclear.

**Return `0` (these do NOT carry a turmoil direction):**
- **Diplomatic logistics**: where/when/whether leaders *meet*, hold a summit, or
  have a call. The venue/scheduling of talks carries no direction. (A concrete
  signed *agreement* — an actual ceasefire/peace deal — is `-1`; merely
  meeting/talking is `0`.)
- **Sports / esports / video games**: any match, tournament, or game result, even
  when the title contains a crisis word ("strike", "war", "siege", "attack") or a
  country name.
- **Routine politics / normal economic data**: a regularly scheduled election and
  its outcome, an ordinary end-of-term leadership transition, a routine policy /
  rate decision, or an ordinary market-level / GDP reading that is not a crisis.

Judge the resolution criterion, not the mere presence of a country name, a
leader, or a crisis-sounding word. A market that clearly moves turmoil up or down
must be `+1` or `-1`; reserve `0` for markets that genuinely carry no direction.

Market title: {market_title}
Market description: {market_description}
