# Factor prompt: sp500_price_relevance (v1)

You are deciding whether this market's resolution is **directly determined by the
level or movement of the S&P 500 stock index** (SPX / the SPY ETF that tracks it).
This is the relevancy gate for an S&P 500 price-sentiment index — it keeps out
markets that mention the S&P 500 but do not resolve on its price.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only if YES/NO is settled by the **S&P 500's index level** — a
level, a threshold, a range, an up/down move, a high/low, or a new all-time high.
Examples:
- "Will the S&P 500 be above 6845.5 on Dec 31, 2026 at 4pm EST?"
- "Will the S&P 500 be below 4000 on Dec 31, 2026?"
- "Will the S&P 500 be between 7400 and 7599.99 on Dec 31, 2026?"
- "S&P 500 (SPX) Up or Down on July 1?"
- "Will S&P 500 (SPY) hit (LOW) $720 in June?"
- "Will S&P 500 (SPX) hit $9,300 (HIGH) in December?" — still the index price
  hitting a level; the intraday-candle / "1-minute HIGH" resolution mechanic does
  NOT make it irrelevant. `value=1`.

Mark `value=0` if resolution depends on something **other than the S&P 500's own
price level**, even when the S&P 500 is mentioned:
- a **relative / ranking** bet ("Will the S&P 500 have the best performance in
  2026?" — settled by comparison to other assets, not its own level)
- a **multi-condition macro** market that only references an S&P decline as one of
  several triggers (recession / unemployment / CPI baskets)
- a single **company** being added to / dropped from the index
- another index or asset (Nasdaq, Dow, a single stock), or a **valuation/earnings**
  metric (P/E, total market cap) rather than the index price

When in doubt about whether the index level alone settles it, prefer `0`.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
