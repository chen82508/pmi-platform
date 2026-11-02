# Factor prompt: republican-on-yes (v1)

You are evaluating whether **a YES resolution on this market corresponds to
the Republican / GOP candidate winning** — i.e. whether the market's price
should be read as P(Republican wins).

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` if YES on this market means **Republican / GOP wins** the seat
in question. Examples:
- "Will the GOP win the Ohio Senate seat in 2026?" → YES = R wins → value=1
- "Will <named Republican candidate> win NV Senate 2026?" → YES = R wins → value=1
- "Will Republicans hold AZ-06 in 2026?" → YES = R wins → value=1

Mark `value=0` if YES on this market means **Democrats / a non-Republican wins**,
or if the polarity is the opposite of Republican victory. Examples:
- "Will Democrats flip the Ohio Senate seat in 2026?" → YES = D wins → value=0
- "Will <named Democratic candidate> win NV Senate 2026?" → YES = D wins → value=0

When the market is multi-outcome (e.g. has individual outcomes per candidate
rather than a single YES/NO), evaluate the outcome currently being scored —
the framework will tell you which outcome's price is being passed in.

Mark `value=0` with low confidence (0.1–0.3) if you cannot determine party
polarity from the title + description.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
