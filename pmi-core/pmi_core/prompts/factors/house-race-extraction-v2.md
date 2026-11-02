# Factor prompt: house-race-extraction (v2)

You are extracting structured race information from a prediction-market
listing. The question: **is a YES resolution on this market equivalent to
"party X wins the US House seat for congressional district D in the November
2026 general election"?** If so, extract which party, which state, and which
district number.

Recognize the compact district format many venues use: a title like
"CA-29 General Election — Luz Maria Rivas" or "OH-07 General Election —
Max Miller" is the US House race for that state's Nth congressional district
— the "XX-NN" token is a two-letter state abbreviation + district number
(CA-29 = California, district 29; OH-07 = Ohio, district 7), even when the
words "House"/"District"/"Representatives" do not appear. The trailing name
after the dash is a candidate.

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

**Party from a named candidate.** When YES means a named candidate wins, use
your own knowledge of that candidate to determine their major party (R or D)
— do NOT bail to value=0 merely because the party is absent from the title. A
candidate winning means their party takes the seat, so set race_type=candidate
and party = the candidate's party. Only fall back to value=0 if you genuinely
cannot identify the candidate's major-party affiliation.

Examples:
- "Will the Republican party win the California's 20th District general election for the US House of Representatives in 2026?"
  → value=1, state=California, district=20, party=R, race_type=party_direct
- "OH-07 General Election — Max Miller" → value=1, state=Ohio, district=7,
  party=R, race_type=candidate (Max Miller is the Republican incumbent)
- "TX-35 General Election — Greg Casar" → value=1, state=Texas, district=35,
  party=D, race_type=candidate

Mark `value=0` for: primaries / nominations; margin / vote-count markets;
chamber-level markets ("control the US House", "how many House seats"); US
Senate / governor / any non-House race; foreign or state legislatures;
independent / third-party candidates; and any market where you cannot
determine the state, the district, or the candidate's major-party affiliation.

## Market

Title: {market_title}
Description: {market_description}
Venue: {market_venue}

Return the JSON only — no extra commentary.
