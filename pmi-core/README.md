# pmi-core

The engine and the shared database layer. Every other Python service imports
it.

```
pmi_core/
  dsl/           YAML index definition → pydantic IR (ir.py) + JSON Schema
  engine/        selector → Tier 0 pre-filter → factor evaluation → aggregator
                 (pipeline.py), plus formulas: seat_distribution (Poisson-
                 binomial), moneyness, bucket_collapser, geo rollup, backtest,
                 tick_guard, and the computed boards (markets, gdp, climate…)
  llm/           provider abstraction: OpenAI-compatible endpoints, Ollama and
                 a fixture-label replayer, routed by model-id prefix;
                 deterministic stub fallback
  eval/          classification eval harness (stratified sampling, multi-model
                 labelling, sample / population / liquidity-weighted metrics)
  models/        SQLAlchemy models (tier-prefixed: core_ / ts_ / audit_ / vec_)
  vectorstore/   pgvector helpers for the semantic selector and Tier 0
  index_defs/    the current index definitions (YAML)
  prompts/       versioned factor prompts (markdown, append-only)
  queue.py       Postgres job queue (SKIP LOCKED + LISTEN/NOTIFY)
  workflow.py    step-checkpointed durable workflows
  cli/           the `pmi-core` command
alembic/         schema migrations
```

## Pipeline

1. **Select.** Keyword, category and semantic (pgvector) selectors build the
   candidate set, after venue and money-type filters and a per-index cap.
2. **Tier 0.** Candidates whose embedding falls below the cosine floor
   against the index anchor are dropped before any LLM call.
3. **Tier 1.** Each factor is resolved through the model registry
   (`core_factor_models`) to a prompt, model and temperature, and evaluated
   concurrently. Results are written to `audit_evaluations`, which is
   immutable and keyed for caching.
4. **Aggregate.** Liquidity quantile weights, optional moneyness kernels and
   bucket collapsing feed the declared formula. The result is written to
   `ts_index_scores` with the ids of the evaluations it used.

## CLI

```bash
pmi-core migrate                      # alembic upgrade head
pmi-core seed                         # fixture markets + register index defs
pmi-core demo bind|history            # fixture-provider demo (see pmi-demo)
pmi-core score war-index-2026         # one tick, persisted
pmi-core dry-run <def.yaml>           # in-process against fixtures, no DB/LLM
pmi-core models list|register|promote|doctor
pmi-core eval sample|label|score      # classification quality
pmi-core keys create|list|revoke      # API keys for pmi-api
```

In the compose stack:
`docker compose --profile pmi run --rm pmi-core <command>`.

## Tests

```bash
../.venv/bin/pytest -q     # fully mocked; no database needed
```
