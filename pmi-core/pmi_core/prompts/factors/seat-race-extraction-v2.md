# Factor prompt: seat-race-extraction (v2)

You are extracting structured race information from a prediction-market
listing. The question: **is a YES resolution on this market equivalent to
"party X wins a single statewide US race — a US Senate seat OR a state
Governorship — in state Y in the November 2026 general election"?** If so,
extract which party, which state, and which office.

This prompt is office-aware: decide the office from the title itself (a
"Senate" title → a US Senate seat; a "Governor"/"Gubernatorial" title → that
state's Governorship). Both are single statewide seats and are extracted the
same way.

Return JSON with shape:
```json
{
  "value": 0 or 1,
  "state": "<full US state name, e.g. Ohio>",
  "party": "R" or "D",
  "office": "senate" or "governor",
  "race_type": "party_direct" or "candidate",
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}
```
When `value=0`, omit `state` / `party` / `office` / `race_type`.

Mark `value=1` and fill the fields when YES maps cleanly to one party taking
one state's Senate seat or Governorship.

**Party from a named candidate (important).** Many venues list one market per
candidate and do NOT write the party in the title (e.g.
"Wyoming Senate General Election — Harriet Hageman"). When YES means a *named
candidate* wins, use your own knowledge of that candidate to determine their
party (R or D) — do NOT return `value=0` merely because the party is absent
from the title. A candidate winning means their party takes the seat, so set
`race_type=candidate` and `party` = the candidate's party. Only fall back to
`value=0` if you genuinely cannot identify the candidate or their major-party
affiliation.

Examples:
- "Will the Republicans win the Ohio Senate race in 2026?"
  → value=1, state=Ohio, party=R, office=senate, race_type=party_direct
- "Which party will win the 2026 US Senate election in Maine? - Democratic"
  → value=1, state=Maine, party=D, office=senate, race_type=party_direct
- "Wyoming Senate General Election — Harriet Hageman"
  → value=1, state=Wyoming, party=R, office=senate, race_type=candidate
  (Hageman is a Republican; her winning means R takes the Wyoming seat)
- "Hawaii Governor General Election — Josh Green"
  → value=1, state=Hawaii, party=D, office=governor, race_type=candidate
  (Green is a Democrat; his winning means D takes the Hawaii governorship)
- "Will the Republican party win the Texas general election for US Senate in 2026?"
  → value=1, state=Texas, party=R, office=senate, race_type=party_direct

Mark `value=0` for everything else, including:
- primaries / nominations ("...win the Texas Republican Senate primary...")
- margin-of-victory or vote-count markets ("...margin of victory... at least
  5 percentage points?")
- chamber-level markets ("How many Senate seats will the GOP control...",
  "...win a US Senate Majority...", "Democrat Seats in the Senate — At Least 45")
- US House / Representative / district races (a different grammar handles those)
- any race outside 2026 (a special election resolving in a different cycle)
- foreign senates / governors (Australia, Italy, Philippines, ...)
- state legislatures ("...Pennsylvania State Senate...")
- independent / third-party candidates (neither R nor D — no major party to credit)
- any market where you cannot determine the state, the office, or the
  candidate's major-party affiliation

## Market

Title: {market_title}
Description: {market_description}
Venue: {market_venue}

Return the JSON only — no extra commentary.
