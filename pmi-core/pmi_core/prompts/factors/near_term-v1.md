# Factor: near_term (v1)

`value=1` iff the market resolves within 90 days of today. Long-dated (resolution > 90d away) → 0.

Return JSON: `{ "value": 0|1, "confidence": 0.0..1.0, "reasoning": "..." }`.

Title: {market_title}
Description: {market_description}
Resolves at: {closes_at}
Today: {today}
