# Factor prompt: btc_major_move (v1)

You are deciding whether a **YES** resolution represents a **substantive**
Bitcoin price move or a **decisive price level**, as opposed to high-frequency
microstructure noise. This down-weights markets that are technically about price
but carry little sentiment signal, so the index reflects conviction.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` for a **meaningful** move or level:
- a daily / multi-day / weekly / monthly horizon ("above $64,000 on June 30?",
  "exceed $140,000 in 2026?")
- a round-number or all-time-high milestone ("Bitcoin all time high by ...?")
- a notable rally or decline (a several-percent move, a crash, a breakout)

Mark `value=0` for **micro / negligible** outcomes:
- **sub-hourly** ticks ("Bitcoin Up or Down - June 24, 2:55PM-3:00PM ET")
- **narrow range bins** a few hundred dollars wide ("$62,100 to $62,199.99")
- any outcome whose move is so small or so short-dated that it is essentially a
  coin-flip on noise rather than a directional view

If the market is clearly a real directional or level bet but you are unsure of
the magnitude, prefer `1`.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
