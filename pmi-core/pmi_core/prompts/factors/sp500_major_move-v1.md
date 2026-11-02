# Factor prompt: sp500_major_move (v1)

You are deciding whether a **YES** resolution represents a **substantive** S&P 500
move or a **decisive index level**, as opposed to high-frequency microstructure
noise. This down-weights markets that are technically about price but carry little
sentiment signal, so the index reflects conviction.

Return JSON with shape:
```json
{ "value": 0 or 1, "confidence": 0.0..1.0, "reasoning": "<one sentence>" }
```

Mark `value=1` for a **meaningful** move or level:
- a daily-close / multi-day / weekly / monthly / year-end horizon ("above 6845.5
  on Dec 31, 2026?", "below 4000 by year-end?")
- a round-number or all-time-high milestone ("S&P 500 hits a new record?")
- a notable rally or decline (a several-percent move, a correction, a crash, a
  breakout)

Mark `value=0` for **micro / negligible** outcomes:
- **sub-hourly** or same-session tick bets ("S&P 500 (SPX) Opens Up or Down?",
  an intraday up/down window)
- **narrow range bins** that are only a fraction of a percent wide and are
  essentially a coin-flip on noise
- any outcome whose move is so small or so short-dated that it reflects
  microstructure rather than a directional view

If the market is clearly a real directional or level bet but you are unsure of the
magnitude, prefer `1`.

## Market

Title: {market_title}
Description: {market_description}

Return the JSON only — no extra commentary.
