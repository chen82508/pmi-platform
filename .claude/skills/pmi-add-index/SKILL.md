---
name: pmi-add-index
description: >-
  How to add or change a PMI index in this repo: the YAML definition
  (selectors, factors, weighting, aggregation formula), factor prompts, fixture
  markets with their judgements, dry-run iteration, seeding, model binding, and
  getting the index into the demo and its end-to-end check. Use whenever a task
  touches pmi-core/pmi_core/index_defs/, prompts/factors/, a factor's model
  binding, or pmi-demo/generate_fixtures.py — including "make index X show a
  score", "add a factor", "tune weights", or "change a prompt".
---

# Add or change an index

An index is data: one YAML file parsed into `IndexDef`
(`pmi-core/pmi_core/dsl/ir.py`). A new index needs no engine code unless it
needs a **new formula or selector type**, which is engine work in
`engine/aggregator.py` / `engine/selector.py` and out of scope here.

```
1 YAML ─▶ 2 prompts (new factors only) ─▶ 3 fixtures ─▶ 4 dry-run (iterate here)
      ─▶ 5 seed ─▶ 6 bind models ─▶ 7 score / history ─▶ 8 verify + e2e
```

## 1. The YAML

Start from the closest existing definition in `pmi-core/pmi_core/index_defs/`:

| Template | Shows |
|---|---|
| `btc-bull-bear-index.yaml` | `signed_weighted_average_x_100`, moneyness weighting, semantic anchor |
| `sp500-bull-bear-index.yaml` | same formula without moneyness; the simplest real definition |
| `us-senate-2026-republican-seats.yaml` | `seat_projection_sum` with an `extraction` block (states, parties) |
| `war-index-2026.yaml` | `geo_weighted_mean` (country → region → world), `exclude_terms`, play money |
| `semantic-war-demo.yaml` | the smallest selector set (one semantic anchor) |

| Field | Rules |
|---|---|
| `id`, `version` | `id` is a URL-safe slug and the file name. New index → `version: 1`. **Any later edit, even a comment, needs a version bump**: the YAML's sha256 is the SCD2 key and `seed` raises on a mismatch. |
| `selectors[]` | `keyword` is a Postgres word-boundary regex over titles (pick terms carefully: "strike" also matches Counter-Strike; use `exclude_terms`). `category` matches `core_markets.category`. `semantic` (pgvector cosine to `anchor`, `min_similarity`) also enables the Tier 0 pre-filter; it only runs when `PMI_ACTIVE_EMBEDDING_MODEL` is set and fails open otherwise. |
| `venues`, `include_play_money` | `venues` absent = any venue. Play money is excluded from membership unless `include_play_money: true` (then `weighting.liquidity.play_money_weight` discounts it). |
| `max_markets`, `min_volume_24h` | membership cap and an untraded-market gate applied before the cap. A cap that binds makes membership depend on selector ranking; raise it rather than let it bind. |
| `factors[]` | `type`: `binary` (0/1), `ternary` (−1/0/+1, the direction factor), `score` (0–1), `extraction` (structured JSON). `weight: null` = not part of relevancy (direction and every `extraction` factor). Weights need not sum to 100. `gate: true` (binary only) excludes markets judged 0. |
| `extraction` | `group_by`: `us_state` · `us_house_district` · `us_governor` · `country`; `sides`: `party` · `none` (`country` needs `none`); `methods` tiers run `patterns > candidates > llm_fallback` regardless of list order. |
| `weighting` | `liquidity.method`: `quantile` · `linear` · `none`; optional `moneyness` (strike-aware kernel) for price ladders. |
| `aggregation.formula` | `weighted_average_x_100` (default) · `signed_weighted_average_x_100` · `seat_projection_sum` · `entity_lean_mean` · `geo_weighted_mean`. Any other string silently falls back to the default; dry-run's `formula_used` exposes that. |
| `aggregation.min_components` | below it the tick scores `null` ("no data"). |

The JSON Schema for editors is `pmi_core/dsl/schema/index-def.schema.json`;
regenerate it with `pmi-core schema dump` after changing `ir.py`.

## 2. Prompts (new factors only)

Add `pmi-core/pmi_core/prompts/factors/<factor>-v1.md`, modelled on
`directly_about_war-v1.md`. Rules:

- **Append-only.** To change a prompt, add `-v2.md` and point `prompt_ref` at
  it. Never edit a version that may already be registered (`ensure_prompt`
  raises on a sha mismatch).
- The output contract must match the factor type: JSON with `value`
  (`0|1`, `-1|0|1`, `0..1`, or `1` plus extracted fields), optional
  `confidence`, `reasoning`. The shared parser is `llm/base.parse_factor_response`.
- Placeholders are substituted by `render_prompt`: `{market_title}`,
  `{market_description}`, `{market_category}`, `{market_tags}`,
  `{market_venue}`, `{market_slug}`, `{market_external_id}`. Literal braces
  (JSON examples) are left alone.

## 3. Fixtures

There is no live data here: the index scores only what
`pmi-demo/generate_fixtures.py` produces. Add markets there and regenerate:

```bash
python3 pmi-demo/generate_fixtures.py     # never hand-edit fixtures/markets.json
```

- Titles must parse the way real venue titles do: the patterns tiers
  (`engine/seat_mapping.parse_seat_race`, `engine/geo.match_countries`,
  `engine/moneyness.extract_strike`) are the real grammars. Check a title in
  a REPL before relying on it.
- `judgements` maps **factor id → answer** (scalar = `{"value": x}`; extraction
  = `{"value": 1, "country": "Ukraine"}`). Unlisted factors answer 0, which is
  what an off-topic market should get, so include a distractor or two.
- `price_history` (14 daily prices before `last_price`) drives the history
  chart and backtests; `closes_in_days` keeps markets open regardless of date.
- Moneyness ladders vote only with at least `min_ladder_size` same-venue,
  same-close strikes whose direction is +1.

## 4. Dry-run — iterate here

```bash
just dry-run pmi_core/index_defs/<id>.yaml
```

In-process, no DB, no LLM; uses the fixture judgements (stub hash for markets
without them), so its score equals what the stack will publish. Read:
`selectors_candidates` (0 = selectors too narrow), `component_count`,
`score`, and `formula_declared` vs `formula_used` (a mismatch means a
fallback). Tune selectors, weights and fixtures until this looks right.

## 5–7. Seed, bind, score

Run `pmi-core` inside the stack as
`docker compose --profile pmi run --rm pmi-core <command>`:

```bash
pmi-core seed                         # registers every top-level YAML (SCD2)
pmi-core demo bind                    # binds unbound factors to fixture/labels
docker exec pmi-worker pmi-workers run tag-races   # election defs only
pmi-core score <id>                   # one tick → ts_index_scores
pmi-core demo history <id> --days 14  # a 14-day series for charts
```

`demo bind` never replaces an existing production binding. For a real model:

```bash
pmi-core models register --factor <factor> --prompt-name factors/<factor> \
    --prompt-version 1 --llm gpt-4o-mini-2024-07-18 --temperature 0.1
pmi-core models promote <model_id> --stage production
pmi-core models doctor --strict       # fails if any declared factor is unbound
```

Changing a model or temperature is a new `register` + `promote`, never an
in-place update. The YAML must be committed: scoring reads the file from disk
for every definition the DB lists as current.

## 8. Verify

- API: `/indexes/<id>/score`, `/score/history`, `/explain` (the drivers;
  the first place to look when a score surprises you).
- Dashboard: `http://localhost:3000/pmi_dashboard/indexes/<id>`.
- To make it part of the demo, add the id to `DEMO_INDEXES` in
  `pmi-core/pmi_core/cli/pipeline.py`, update the expected count in
  `scripts/demo_assertions.sql`, and run `just e2e`.
- Run `pmi-core`'s tests if you touched engine code, and `just e2e` if you
  changed fixtures (all demo numbers move together).
