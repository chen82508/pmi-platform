# Factor prompt: sp500_direction (v1)

Determine what a **YES** resolution on this market would mean for the **S&P 500**
index level — **up (bullish)**, **down (bearish)**, or **neither**.

Return JSON:
```json
{ "value": -1 | 0 | 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

- `+1` = YES means the S&P 500 went **UP** / is **higher** / **above** a level /
  **closed up** / made a **new high**. (e.g. "Will the S&P 500 be above 6845.5 on
  Dec 31, 2026?", "S&P 500 (SPX) Up or Down ...?" resolving on **Up**, "Will the
  S&P 500 hit a new all-time high ...?")
- `-1` = YES means the S&P 500 went **DOWN** / is **lower** / **below** a level /
  **fell / dropped / corrected / crashed**. (e.g. "Will the S&P 500 be below 4000
  on Dec 31, 2026?", "Will the S&P 500 (SPY) hit (LOW) $720 in June?", "S&P 500
  Up or Down ...?" resolving on **Down**, "S&P 500 down 10% this year?")
- `0` = no clear directional meaning. Return `0` for ANY of these, no matter
  where the number sits relative to today's level:
  - a **range / between** market ("Will the S&P 500 be between 7400 and 7599.99
    on Dec 31, 2026?", "between 4000 and 4199.99"). A range is ALWAYS `0` — do NOT
    infer bearish just because the range is far below the current level, or
    bullish because it is above. A range names a band, not a direction.
  - an **absolute / either-direction move** market: "absolute percentage change
    ... at least X%", "moves by X% or more", "S&P 500 up or down by 2%",
    realized-volatility bets. YES resolves whether the index moves UP **or** DOWN,
    so it carries no directional meaning → `0`. Do not read "absolute change" as
    "higher".
  - a **relative** comparison ("Will the S&P 500 have the best performance in
    2026?" — settled vs other assets), or anything else where YES does not
    clearly imply up vs down.

Judge by the **literal resolution condition**, not by whether you think the move
is likely. A high threshold unlikely to be hit is still `+1` (YES would mean the
index rose to it). A "below X" strike far under spot is still `-1` (YES = index
fell to it). But a two-sided **range** or an **absolute-change** market is `0`
even when the numbers are far from today. Pick the side the YES outcome describes;
if YES can be satisfied by a move in either direction, that is `0`.

Market title: {market_title}
Market description: {market_description}

Return the JSON only — no extra commentary.
