# Factor prompt: about_turmoil (v1)

You are evaluating whether a prediction market is **directly about instability,
crisis, or turmoil affecting a country** — the primary on-topic gate for the
Global Instability ("War") Index. The index measures a country's overall level of
turmoil, not only interstate war.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` if the market's **resolution criterion** is directly about any of:
- **Armed conflict / war** — an interstate or civil war starting, escalating,
  ending, or reaching a milestone; strikes, invasions, troop movements, a signed
  ceasefire/peace agreement.
- **Civil unrest** — protests, riots, mass demonstrations, uprisings, insurrection,
  general strikes that threaten order.
- **State security crisis** — a state of emergency, state of siege, martial law,
  nationwide curfew, military/police deployment against the population, a violent
  crackdown.
- **Coup / regime change by force** — a coup or attempted coup, a government
  overthrown.
- **Terrorism** — terrorist attacks, major bombings, political assassinations.
- **Political crisis** — a government collapsing, a no-confidence vote toppling a
  government, an emergency dissolution of parliament, or a head of state/government
  losing power EARLY (ousted, forced to resign, toppled, or leaving before the end
  of their term). This INCLUDES markets on how long an embattled leader/government
  will last, whether a prime minister survives, or when a country will get a new
  leader amid a government crisis. "Macron leaves office before Jan 1", "French
  confidence vote fails", and "How long will the French PM last?" are all `value=1`.
- **Severe economic crisis** — hyperinflation, a currency collapse, a sovereign
  debt default, a banking/financial collapse, or an emergency IMF bailout.

Mark `value=0` if the market is about:
- **Routine politics** — a regularly scheduled election and its outcome, an
  ordinary end-of-term leadership transition, cabinet reshuffles, ordinary
  legislation, budgets, treaties, or sanctions enacted as normal policy.
  (An incumbent simply winning or losing a scheduled election is NOT turmoil; a
  leader being forced out early or a government collapsing IS.)
- **Diplomacy logistics** — whether, where, or when leaders meet, hold a summit,
  or have a call. The venue/scheduling of a meeting is not turmoil even between
  parties to a war.
- **Normal economic data** — stock-index levels, GDP prints, a routine central-bank
  rate decision, or an ordinary inflation reading that is not a crisis.
- **Sports, esports, or entertainment** whose title merely contains a crisis word
  ("strike", "war", "siege", "attack") or a country name.
- A market that merely **mentions** a country or leader without its resolution
  turning on that country's instability.

The mere presence of a country name, a leader, or a crisis-sounding word does NOT
make a market about turmoil — judge the resolution criterion.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
