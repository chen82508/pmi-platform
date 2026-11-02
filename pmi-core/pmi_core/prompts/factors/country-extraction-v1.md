# Factor prompt: country-extraction (v1)

You are extracting the **primary country** a prediction-market listing is about,
for a geopolitical war / armed-conflict index. The question: **which single
country is most directly the subject of this market's conflict, war, military,
or security event?**

Return JSON with shape:
```json
{
  "value": 0 or 1,
  "country": "<one country, e.g. Ukraine, North Korea, DR Congo>",
  "confidence": 0.0..1.0,
  "reasoning": "<one sentence>"
}
```
When `value=0`, omit `country`.

Mark `value=1` and fill `country` when the market is about an armed conflict,
war, military action, strike, invasion, ceasefire, coup, nuclear/weapons event,
or territorial-control event involving a specific country.

Choosing the **primary** country when several are named:
- The country whose **territory the conflict is on**, or whose **sovereignty /
  security is most directly at stake**, is primary.
  - "Will Russia invade Ukraine in 2026?" → **Ukraine** (the territory at stake)
  - "Will Israel strike Iran in 2026?" → **Iran** (the target of the action)
  - "Will there be a ceasefire in Gaza by 2026?" → **Gaza**
  - "Will North Korea test a nuclear weapon in 2026?" → **North Korea**
  - "Will the US send troops to Taiwan?" → **Taiwan** (where the action lands)
- Prefer the more specific territory (Gaza / West Bank / Taiwan) over a larger
  state when the market is about events there.
- Use the country's common English name; the consumer canonicalises it.

Mark `value=0` for everything else, including:
- markets with no country-specific conflict subject (global/abstract: "Will
  WW3 happen?", "Will any nuclear weapon be used in 2026?")
- elections, economics, sports, crypto, weather — anything not a
  country's armed-conflict / military / security event
- markets where you genuinely cannot identify one primary country

## Market

Title: {market_title}
Description: {market_description}
Venue: {market_venue}

Return the JSON only — no extra commentary.
