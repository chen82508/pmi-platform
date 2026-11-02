# Factor prompt: house-race-extraction (v1)

You are extracting structured race information from a prediction-market
listing. The question: **is a YES resolution on this market equivalent to
"party X wins the US House seat for congressional district D in the November
2026 general election"?** If so, extract which party, which state, and which
district number.

Return JSON with shape:
```json
{
  "value": 0 or 1,
  "state": "<full US state name, e.g. California>",
  "district": <district number as an integer, or "at-large">,
  "party": "R" or "D",
  "race_type": "party_direct" or "candidate",
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}
```
When `value=0`, omit `state` / `district` / `party` / `race_type`.

Mark `value=1` and fill the fields when YES maps cleanly to one party taking
one congressional district's House seat in the 2026 general election.
Examples:
- "Will the Republican party win the California's 20th District general
  election for the US House of Representatives in 2026?"
  → value=1, state=California, district=20, party=R, race_type=party_direct
- "Will Democratic Primary Winner win the Alabama 5th District general
  election for the US House of Representatives in 2026?"
  → value=1, state=Alabama, district=5, party=D, race_type=party_direct
  (the primary winner IS the nominee, so YES means that party takes the seat)
- "Will Adam Gray win the California 13th District general election for the
  US House of Representatives in 2026?" → value=1, state=California,
  district=13, party=D, race_type=candidate (the candidate winning means
  their party takes the seat — only set party if you know the candidate's
  party with confidence; otherwise value=0)
- An at-large district (states with one House seat) →
  district="at-large".

Mark `value=0` for everything else, including:
- primaries / nominations ("...win the Texas 9th District Republican primary
  election...")
- margin-of-victory or vote-count markets ("...margin of victory... at least
  5 percentage points?")
- chamber-level markets ("Which party will control the US House...", "How
  many House seats will the GOP win...")
- US Senate, governor, or any non-US-House race
- foreign legislatures
- state legislatures ("...California State Assembly...")
- independent candidates (neither R nor D — there is no party to credit)
- any market where you cannot determine the state, the district, or the
  party polarity

## Market

Title: {market_title}
Description: {market_description}
Venue: {market_venue}

Return the JSON only — no extra commentary.
