# Factor prompt: seat-race-extraction (v1)

You are extracting structured race information from a prediction-market
listing. The question: **is a YES resolution on this market equivalent to
"party X wins the US Senate seat of state Y in the November 2026 general
election"?** If so, extract which party and which state.

Return JSON with shape:
```json
{
  "value": 0 or 1,
  "state": "<full US state name, e.g. Ohio>",
  "party": "R" or "D",
  "race_type": "party_direct" or "candidate",
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}
```
When `value=0`, omit `state` / `party` / `race_type`.

Mark `value=1` and fill the fields when YES maps cleanly to one party taking
one state's Senate seat. Examples:
- "Will the Republicans win the Ohio Senate race in 2026?"
  → value=1, state=Ohio, party=R, race_type=party_direct
- "Which party will win the 2026 US Senate election in Maine? - Democratic"
  → value=1, state=Maine, party=D, race_type=party_direct
- "Will <named Democratic candidate> win the Nevada general election for US
  Senate in 2026?" → value=1, state=Nevada, party=D, race_type=candidate
  (the candidate winning means their party takes the seat)

Mark `value=0` for everything else, including:
- primaries / nominations ("...win the Texas Republican Senate primary...")
- margin-of-victory or vote-count markets ("...margin of victory... at least
  5 percentage points?")
- chamber-level markets ("How many Senate seats will the GOP control...",
  "...win a US Senate Majority...")
- US House, governor, or any non-US-Senate race
- foreign senates (Australia, Italy, Philippines, ...)
- state legislatures ("...Pennsylvania State Senate...")
- independent candidates (neither R nor D — there is no party to credit)
- any market where you cannot determine the state or the party polarity

## Market

Title: {market_title}
Description: {market_description}
Venue: {market_venue}

Return the JSON only — no extra commentary.
