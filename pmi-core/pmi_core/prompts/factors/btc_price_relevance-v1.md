# Factor prompt: btc_price_relevance (v1)

You are deciding whether this market's resolution is **directly determined by
Bitcoin's spot price** (its level or its movement). This is the relevancy gate
for a BTC price-sentiment index — it keeps out markets that mention Bitcoin but
do not resolve on its price.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if YES/NO is settled by **BTC's price** — a level, a
threshold, a range, an up/down move, a high/low, or a new all-time high. Examples:
- "Bitcoin above $64,000 on June 24?"
- "Bitcoin price on Jun 23, 2026? - $55,900 or above"
- "Will Bitcoin's price exceed $63,500 ...?"
- "Bitcoin Up or Down - <window>"
- "Bitcoin all time high by June 30, 2026?"
- "Will the price of Bitcoin be between $62,000 and $64,000?"

Mark `value=0` if resolution depends on something **other than BTC's price**,
even when Bitcoin is the subject:
- a person/company **mentioning** or **adopting** Bitcoin ("Will Trump say
  Bitcoin?", "Will <company> add BTC to its treasury?")
- **regulation / ETF approval / legal** events
- **on-chain or network** metrics (hash rate, fees, halving, kimchi premium)
- another asset's price, or BTC **dominance / ratio** vs other coins

When in doubt about whether the dollar price settles it, prefer `0`.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
