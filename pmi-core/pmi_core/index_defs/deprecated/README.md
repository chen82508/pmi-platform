# Deprecated index definitions

YAMLs here are **retired** — `pmi-core seed` and the pipeline only glob
`index_defs/*.yaml` (non-recursive), so nothing in this directory registers.
The files stay for provenance; their historical `ts_index_scores` rows remain
queryable via the SCD2 `core_index_definitions` history.

## us-{senate,house}-2026-republican-share (retired 2026-07-05, alembic 0016)

Retired for two compounding reasons:

1. **The published number was not a share.** The generic
   `weighted_average_x_100` path maps `score = 50 + raw×50`, so an unsigned
   index whose directions are all +1 can never read below 50 — the senate
   "share" showed 66.4 while the seats index put E[R seats] at 49.1/100 over
   the same reality. The title promised "Republican Seat Share (%)"; the
   number wasn't one.
2. **The component set was noisy.** Both factors were soft weights (an
   `is_senate_race_2026=0` market still entered at 0.4 relevancy — 2,173
   senate / 7,947 house components for ~35 / ~440 races), and the D-side
   polarity flip (1−p for "Democrat wins on YES" markets) was a documented
   open gap since v1.

**Replacement:** derive share from the seats indexes, which already carry the
per-race extraction, polarity handling and Poisson-binomial fold:
`share % = E[R seats] / chamber size × 100`
(`us-senate-2026-republican-seats` / 100, `us-house-2026-republican-seats` / 435).

If a standalone share index is ever wanted again, it needs (a) a raw×100
formula, (b) `gate: true` on the race-membership factor, and (c) the polarity
flip — i.e. everything the seats path already does.
