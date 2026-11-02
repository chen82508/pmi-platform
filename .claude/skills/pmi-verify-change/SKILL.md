---
name: pmi-verify-change
description: >-
  Which checks a change in this repo needs before it is done, how to run each
  exactly the way CI does, how to make a running stack pick up the change, and
  how to read the usual failures (hanging tests, 401s, empty scores, stale
  dashboard). Use before reporting any code change as finished, when CI fails,
  when a test suite hangs or errors locally, or when asked to "run the tests",
  "check CI", or "make sure nothing broke".
---

# Verify a change

CI (`.github/workflows/ci.yml`) is the definition of done. Every job has a
local equivalent, and `just ci` runs all of them in sequence.

## 1. Which checks a change needs

| You changed | Run |
|---|---|
| Python in one package | `just lint` · `just test <package>` |
| `pmi-core` (shared by every service) | `just test-all` |
| Scoring, aggregation, selectors, fixtures, the demo flow, `seed` | the above **and** `just e2e` |
| An index YAML or prompt | `just dry-run <yaml>` · `just e2e` (see `pmi-add-index`) |
| A job, its lane, or `cron/crontab` | `just test pmi-workers` (lane table is pinned) |
| `pmi-web` | `just web-check` (tsc, vitest, `next build`) |
| `infra/terraform` | `just tf-check` · tflint · Lambda tests (see `pmi-infra-terraform`) |
| `docker-compose.yml` / `.env.example` | `docker compose --profile pmi --profile web --profile ollama config -q` with `cp .env.example .env` |
| Anything user-visible in the dashboard | rebuild pmi-web and look at it; `just screenshots` if README images change |

Refactors must not move behavior: the existing tests stay green unchanged.
Extracted helpers are pure, typed, and get a happy-path and a failure-path test.

## 2. One-time setup

```bash
just venv        # .venv with every Python package, editable, plus dev tools
just test-db     # pgvector on localhost:55432 (user/password: pmi)
cp .env.example .env
```

## 3. Running tests the way CI does

`just test <pkg>` sets the environment the suites need:

| Variable | Why |
|---|---|
| `PMI_TEST_DB_HOST=localhost`, `PMI_TEST_DB_PORT=55432` | api/workers suites `CREATE DATABASE` a throwaway DB on this server |
| `PMI_MLFLOW_ENABLED=false` | otherwise pipeline tests retry a missing MLflow and hit the timeout |
| `PMI_API_REQUIRE_AUTH=false` | the API defaults to requiring a key; route tests then get 401 |

The end-to-end check, `just e2e`, recreates the stack's database volume,
runs the full fixture flow (migrate → seed → `demo bind` → `tag-races` →
`demo history`), and asserts with `scripts/demo_assertions.sql`. Its expected
output is in the README; any change to fixtures or scoring moves those numbers,
so update the README in the same change.

## 4. Making a running stack pick up a change

| Changed | Do |
|---|---|
| Python in pmi-core / api / workers / mcp / notify | `docker restart <container>` (source is bind-mounted) |
| A Python dependency or a Dockerfile | `docker compose --profile pmi up -d --build <service>` |
| pmi-web (production build, nothing mounted) | `docker compose --profile pmi --profile web up -d --build pmi-web` |
| An index YAML or fixtures | `just bootstrap` (or `just e2e` for a clean slate) |
| A migration | `docker compose --profile pmi run --rm pmi-core migrate` |

Worker containers: `pmi-workers` (scheduler), `pmi-worker` (default lane),
`pmi-worker-batch`, `pmi-worker-snapshot`, `pmi-worker-long`.

## 5. Reading failures

| Symptom | Cause |
|---|---|
| A pipeline test hangs, then times out | `PMI_MLFLOW_ENABLED` not `false` |
| Route tests return 401 | `PMI_API_REQUIRE_AUTH` not `false` |
| `connection refused` on 55432 | `just test-db` not running (or a container from another project holds the port) |
| A test passes alone, fails in the suite | process-level caches (`search_cache`, `landing._stats_cache`) leaking; reset them in an autouse fixture |
| An index shows "no data" | a factor bound to the stub (`pmi-core models doctor`), no fixture markets match its selectors, or below `min_components`; check `/indexes/<id>/explain` and `audit_pipeline_runs` |
| An unexpected index has one score point | the `hourly` cron beat scored every definition; expected behavior |
| `selector.semantic_failed` / `tier0.skipped_embed_error` warnings | embeddings are off (`PMI_ACTIVE_EMBEDDING_MODEL` empty); both fail open by design |
| Dashboard shows old UI | pmi-web wasn't rebuilt (production image) |
| A shell variable holding a command does nothing (`$C seed`) | zsh doesn't word-split variables; write the command out or use `just` |

## 6. When CI is red

```bash
gh run list --limit 3
gh run view <run-id> --log-failed | grep -E "error|FAILED|##\[error\]"
```

Reproduce with the matching `just` recipe from §1 before changing anything.
An action reference that fails to resolve (`unable to find version`) means the
action publishes no moving major tag; pin an exact release.
