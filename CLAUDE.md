# PMI Platform — guide for coding agents

Declarative **prediction-market indices**: a YAML definition selects markets,
an LLM judges each market against the index's factors, and the engine weights
and aggregates the judgements into one score with full lineage. This
repository is a portfolio snapshot: live exchange ingestion was removed and a
synthetic fixture world (`pmi-demo/`) replaces it. Read [`README.md`](README.md)
for the product view; this file is how to work in the code.

## Layout

| Path | What lives there |
|---|---|
| `pmi-core/pmi_core/dsl/` | `ir.py`: the `IndexDef` pydantic model every YAML parses into |
| `pmi-core/pmi_core/engine/` | `pipeline.py` (one tick: select → Tier 0 → factor eval → aggregate → persist), `aggregator.py` (the five formulas), `selector.py`, `extraction.py`, `seat_distribution.py`, `moneyness.py`, `backtest.py`, `tick_guard.py`, `dry_run.py` |
| `pmi-core/pmi_core/llm/` | provider routing by model-id prefix (`base.get_provider`): `gpt-*`/`openai/*` → OpenAI-compatible, `ollama/*`, `fixture/*` (replays labels), `stub-*` (in-evaluator hash) |
| `pmi-core/pmi_core/index_defs/` | current index YAML (`deprecated/` is skipped by `seed`) |
| `pmi-core/pmi_core/prompts/factors/` | factor prompts, `<name>-vN.md`, append-only |
| `pmi-core/pmi_core/queue.py`, `workflow.py` | Postgres job queue and durable step-checkpointed workflows |
| `pmi-core/alembic/` | migrations (the schema source of truth) |
| `pmi-workers/` | `@register(name, queue=...)` jobs, `cron/crontab` (beats only enqueue), queue consumers per lane |
| `pmi-api/` | FastAPI routes; `X-API-Key` via `deps.require_api_key` when `PMI_API_REQUIRE_AUTH=true` |
| `pmi-mcp/` | MCP server: thin, agent-shaped wrappers over pmi-api |
| `pmi-web/` | Next.js dashboard under `/pmi_dashboard` (the only UI) |
| `pmi-notify/` | Slack failure-notification library |
| `pmi-demo/` | `generate_fixtures.py` → `fixtures/markets.json` (markets, price history, factor judgements) |
| `infra/terraform/` | EC2 host, CloudWatch alarms, SNS → Lambda → Slack |
| `scripts/` | `demo_assertions.sql` (shared by CI and `just e2e`), `screenshots/` (README images) |

## Commands

Everything goes through the [`justfile`](justfile). Docker runs the stack;
tests and lint run from one host virtualenv (`just venv`).

| Task | Command |
|---|---|
| Full stack (db, mlflow, api, mcp, 4 workers + scheduler, dashboard) | `just up` · `just down` |
| Fixture demo on a running stack | `just bootstrap` |
| Reproduce CI's end-to-end check (fresh stack, ~1 min) | `just e2e` |
| Score one definition in-process, no DB/LLM | `just dry-run pmi_core/index_defs/<id>.yaml` |
| One package's tests (start the DB once with `just test-db`) | `just test pmi-api` |
| Everything CI runs | `just ci` |
| Regenerate README images (after `just e2e`) | `just screenshots` |
| Static dashboard export, as published on GitHub Pages (after `just e2e`) | `just web-static /pmi-platform` |
| pmi-core CLI inside the stack | `docker compose --profile pmi run --rm pmi-core <cmd>` |

DB-backed suites (`pmi-api`, `pmi-workers`) create a throwaway database on the
server given by `PMI_TEST_DB_HOST/PORT`; `just test` sets those for the
`just test-db` container and also sets `PMI_MLFLOW_ENABLED=false` and
`PMI_API_REQUIRE_AUTH=false`. Without them, tests hang on MLflow retries or
fail with 401.

## Invariants — do not break these

1. **Index YAML is versioned (SCD2).** Any change to a YAML file, even a
   comment, changes its sha256; bump `version:` or `seed` refuses it. Never
   edit a `core_index_definitions` row in place.
2. **Prompts are append-only.** Changing a prompt means a new
   `<name>-v<N+1>.md` and pointing `prompt_ref` at it; `ensure_prompt` raises
   if a registered version's text changed.
3. **Evaluations are immutable and cached** on (market, definition, prompt,
   model). A different model or prompt means new rows, never updates.
4. **Model bindings go through the registry** (`pmi-core models register` →
   `promote`). An unbound factor silently falls back to the stub;
   `pmi-core models doctor --strict` is the check.
5. **Jobs declare their lane** in `@register(..., queue=...)` and
   `pmi-workers/tests/test_registry_queues.py` pins the table. A lane with no
   consumer container never runs its jobs.
6. **The fixture world is the only data.** Anything that needs live exchange
   data does not belong here; extend `pmi-demo/generate_fixtures.py` instead
   (regenerate, never hand-edit `markets.json`).
7. **No secrets in the repo or in Terraform state.** The Slack webhook lives in
   an SSM SecureString that Terraform creates with a placeholder and ignores.

## Working conventions

- Match the surrounding code: comments explain *why*, not *what*; incident
  history belongs in commit messages, not comments.
- Refactors keep behavior identical; extracted helpers are pure, typed, and
  get a happy-path and a failure-path test.
- Done means green: `just lint` and the affected package's tests, plus
  `just web-check` for pmi-web or `just tf-check` for Terraform. Changes to
  scoring, fixtures or the demo flow also need `just e2e`.
- pmi-web's container is a production build: after web changes run
  `docker compose --profile pmi --profile web up -d --build pmi-web`. Python
  services bind-mount their source, so `docker restart <container>` is enough.
- Use ruff's per-package config (`pyproject.toml`); don't add blanket ignores.

## Skills

Load the matching skill before starting these tasks:

| Task | Skill |
|---|---|
| Add or change an index definition, factor, prompt or fixture | [`pmi-add-index`](.claude/skills/pmi-add-index/SKILL.md) |
| Decide which checks a change needs; reproduce CI; debug a failing suite | [`pmi-verify-change`](.claude/skills/pmi-verify-change/SKILL.md) |
| Write or review anything that runs on an asyncio loop (api routes, worker loop, mcp) | [`pmi-async-loop-safety`](.claude/skills/pmi-async-loop-safety/SKILL.md) |
| Any pmi-web UI change, or refreshing README screenshots | [`pmi-web-design`](.claude/skills/pmi-web-design/SKILL.md) |
| Terraform, alarms, the Slack notifier, or deploying to AWS | [`pmi-infra-terraform`](.claude/skills/pmi-infra-terraform/SKILL.md) |
