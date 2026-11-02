# Factor prompt: turmoil-direction (v2)

Determine whether a YES resolution on this market would indicate **escalation**
(more turmoil), **de-escalation** (less turmoil), or **neither**. This is the
direction factor for the Global Instability ("War") Index, which measures a
country's overall level of turmoil: armed conflict, civil unrest, state security
crises, coups, terrorism, political crisis, and severe economic crisis.

Return JSON:
```json
{ "reasoning": "<one sentence>", "value": -1 | 0 | 1, "confidence": 0.0..1.0 }
```

- `+1` = YES means turmoil is MORE likely / has escalated. Armed conflict:
  an invasion, strike, troop deployment, missile/nuclear test, war breaking out,
  or a ceasefire / truce ENDING, being cancelled or collapsing.
  Domestic turmoil: protests/riots/unrest erupt; a state of emergency, state of
  siege, or martial law is DECLARED; a coup succeeds or is attempted; a terrorist
  attack occurs; a government collapses or loses a confidence vote; a leader is
  ousted, forced to resign, or leaves office early; hyperinflation / currency
  collapse / sovereign default / financial collapse occurs.

- `-1` = YES means turmoil is LESS likely / has de-escalated. Armed conflict:
  a ceasefire, peace deal, peace agreement, truce, armistice, troop withdrawal,
  or hostage release — and a settlement that ends the fighting even when one side
  concedes (cedes territory, recognizes the other side's control or sovereignty).
  Domestic turmoil: a state of emergency / martial law is
  LIFTED; an embattled government or leader SURVIVES (wins the confidence vote,
  stays in office through the crisis); order is actually restored (the emergency
  ends, the unrest stops); a bailout stabilizes a collapsing economy.
  **A market whose YES is a ceasefire / peace / truce / withdrawal / crisis
  resolution TAKING EFFECT is `-1`, NEVER `0`; one whose YES is that ceasefire
  ENDING or collapsing is `+1`.**

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
- **Sanctions, tariffs and trade measures**: one country imposing, easing or
  LIFTING sanctions or tariffs on another is `0` — it is economic diplomacy and
  says nothing about turmoil inside the country. Do not read lifting sanctions
  as "tensions ease" (`-1`) or imposing them as "tensions rise" (`+1`).
- **Routine politics / normal economic data**: a regularly scheduled election and
  its outcome, an ordinary end-of-term leadership transition, a routine policy /
  rate decision, or an ordinary market-level / GDP reading that is not a crisis.

**Judge what YES reveals about the country's turmoil, not what a measure is
meant to achieve.** A government DECLARING a state of emergency, state of siege,
martial law or a curfew is `+1`, even though the measure aims to stabilize: it
only happens because turmoil has risen.
Only an outcome showing turmoil has already eased (the emergency is LIFTED, order
is restored) is `-1`. The same holds however the market is phrased ("Will X
declare …?", "X declares … by …").

**A concession that settles a war is `-1`.** A side formally recognizing the
other's sovereignty or annexation, or ceding territory, means the fighting is
being settled — less turmoil, even though it is a loss for that side and may be
unpopular. Do not read it as "tensions rise".

**When unsure, return `0`.** If the direction depends on facts you cannot see —
who gains control, whether a foreign deployment is a peacekeeping mission or a
combat one, whether an outcome calms or inflames the situation — or you are not
confident which way YES moves turmoil, return `0`. A wrong sign is worse than no
sign. These are `0`:
- a town, island or region merely changing hands ("no longer under X's
  control", "X captures Y" where it is not an outright invasion): whether that
  calms or inflames the conflict depends on who takes it;
- a peacekeeping / stabilization / monitoring force being announced or deployed,
  unless it enforces an agreed ceasefire or settlement (then `-1`). It is never
  `+1`: it is not a troop deployment for combat.
This never applies to outcomes whose direction is stated outright — those are
always `-1` / `+1`, however vague the timing or the means:
- a war / conflict ENDING, or a ceasefire / peace deal taking effect → `-1`;
- a ceasefire ENDING, or a war breaking out → `+1`;
- unrest, riots, a crisis or violence OCCURRING ("major unrest in X") → `+1`;
- an emergency being declared → `+1`, lifted → `-1`.

Judge the resolution criterion, not the mere presence of a country name, a
leader, or a crisis-sounding word. A market that clearly moves turmoil up or down
must be `+1` or `-1`; one whose direction is unclear is `0`.

Calibration examples (other countries — judge the market below the same way):
- "Will Peru declare a state of emergency in Lima by June 30?" → `+1`
- "Will Peru's state of emergency be lifted by June 30?" → `-1`
- "Sudan truce collapses by March 31?" → `+1`
- "Will there be a ceasefire in Myanmar in 2026?" → `-1`
- "Will there be a second ceasefire between Armenia and Azerbaijan?" → `-1`
- "Will Armenia cede territory to Azerbaijan in a peace deal?" → `-1` (a
  concession that ends the fighting lowers turmoil, even though it is a loss)
- "Will Georgia recognize Russian control of Abkhazia by 2027?" → `-1`
- "Will Moldova recognize Transnistria's independence as part of a settlement?" → `-1`
- "Will an international peacekeeping force deploy to Haiti by June 30?" → `0`
  (could mean worse turmoil or a settlement being enforced)
- "Rebels no longer control Goma by March 31?" → `0` (depends on who takes it
  and how)
- "Will the Sudan civil war end before the 2027 elections?" → `-1`
- "Summer of 2027: major unrest in Kenya?" → `+1`
- "When the Sudan civil war ends, will Sudan attack Chad within a year?" → `+1`
  (YES is a new attack; the condition is only the timing)
- "Will the EU lift sanctions on Belarus by 2027?" → `0`
- "Will the U.S. lift sanctions on Venezuela by March 31, 2027?" → `0` (not
  `-1`: easing sanctions is not turmoil easing)

Market title: {market_title}
Market description: {market_description}
