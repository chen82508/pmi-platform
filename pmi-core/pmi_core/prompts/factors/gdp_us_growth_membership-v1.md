# Factor prompt: gdp_us_growth_membership (v1)

You are deciding whether this market's resolution is **a reading of the United
States' real GDP growth rate for one specific quarter or calendar year**. This is
the membership gate for the US GDP Expectation Index, which folds many such
markets into a single market-implied expected growth number.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` only when **all four** hold:

1. The subject is the **United States** economy (a market that names no country
   at all but is listed on a US venue — e.g. "GDP growth in 2026? - 1.6% to 2.0%"
   — counts as US).
2. It resolves on **real GDP growth**, i.e. the percentage the economy expands or
   contracts, not on a dollar level, not on nominal GDP, and not on a ratio.
3. It pins down **one** period — a single quarter or a single calendar year.
4. The outcome maps to a **growth rate**: a bucket ("2.0% to 2.5%"), a threshold
   ("exceeds 3%", "below 1%"), or a contraction ("negative growth", "the economy
   shrinks").

Examples of `value=1`:
- "Will US GDP growth in 2026 be between 2.0% and 2.5%?"
- "GDP growth in 2026? - 1.6% to 2.0%"
- "Will US real GDP growth for Q2 2026 exceed 3%?"
- "Will the United States have negative real GDP growth in Q1 2027?"
- "Will US real GDP growth in 2026 be below 1%?"

Mark `value=0` when any of these apply:

- **Another country or region.** Any non-US economy is out, however the country
  is written — "Italy's real GDP growth", "Germany's GDP growth", "Japanese GDP
  growth", "China's 2026 GDP", "euro area", "world GDP". Judge the country from
  the meaning of the title, not from whether it names the country the same way
  another market did.
- **Not a growth rate.** GDP in dollars, nominal GDP, GDP per capita,
  debt-to-GDP, deficit-to-GDP, a country's share of world GDP, GDP rankings.
- **A relative comparison with no absolute rate**, e.g. "Will 2027 GDP growth be
  greater than in 2026?" — it resolves on GDP growth but yields no single growth
  number, so it cannot join a distribution.
- **Several periods at once**, e.g. "Will there be negative GDP growth in Q1, Q2,
  or Q3 of 2026?" — it belongs to no single quarter.
- **A compound / conditional bet** where GDP is one of several conditions, e.g.
  "Who wins the presidency in 2028 and will average real GDP growth exceed 3%?"
- **A different economic indicator**, even a closely related one: an NBER
  recession declaration, unemployment, CPI, a stock index level. A market about
  whether the US "enters a recession" is `value=0` **unless** it explicitly
  resolves on the reported real GDP growth figure.

Note on wording: the word "GDP" does **not** have to appear. "Will the US economy
shrink in Q1 2026, as measured by the advance real GDP estimate?" is `value=1`.
Conversely a title containing "GDP" is not automatically in scope — apply the
four tests above.

When in doubt about whether a single US real GDP growth rate settles it, prefer
`0`.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
