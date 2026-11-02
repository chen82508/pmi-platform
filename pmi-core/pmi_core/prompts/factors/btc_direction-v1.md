# Factor prompt: btc_direction (v1)

Determine what a **YES** resolution on this market would mean for Bitcoin's
price — **up (bullish)**, **down (bearish)**, or **neither**.

Return JSON:
```json
{ "value": -1 | 0 | 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

- `+1` = YES means BTC went **UP** / is **higher** / **above** a price level /
  made a **new all-time high** / closed up. (e.g. "Bitcoin above $64,000?",
  "BTC price ... $55,900 or above", "Will Bitcoin exceed $63,500?", "Bitcoin all
  time high by ...?", "Bitcoin Up or Down" resolving on **Up**.)
- `-1` = YES means BTC went **DOWN** / is **lower** / **below** a price level /
  **fell / crashed / dipped**. (e.g. "Will Bitcoin fall below $50,000?", "BTC
  down 10% this week?", "Bitcoin Up or Down" resolving on **Down**.)
- `0` = no clear directional meaning: a **range / between** market ("Bitcoin
  between $62,000 and $64,000?", "$62,100 to $62,199.99"), a symmetric or
  non-price market, or anything where YES does not clearly imply up vs down.

Judge by the **literal resolution condition**, not by whether you think the move
is likely. A high threshold that is unlikely to be hit is still `+1` (YES would
mean price rose to it). Pick the side the YES outcome describes.

Market title: {market_title}
Market description: {market_description}

Return the JSON only — no extra commentary.
