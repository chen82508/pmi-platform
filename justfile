# PMI Platform task runner. `just` lists recipes.
set dotenv-load := false

root_dir := justfile_directory()
compose := "docker compose --profile pmi"

default:
    @just --list

# ── stack ────────────────────────────────────────────────────────────────────

# Start db + mlflow + api + mcp + workers (and the dashboard).
up:
    {{compose}} --profile web up -d --build

down:
    {{compose}} --profile web --profile ollama down

logs SERVICE='pmi-api':
    docker compose logs -f {{SERVICE}}

# Optional local LLM + embeddings (pulls the models the defaults reference).
ollama-up:
    docker compose --profile ollama up -d
    docker exec pmi-ollama ollama pull gemma3:4b
    docker exec pmi-ollama ollama pull nomic-embed-text

# ── pmi-core ─────────────────────────────────────────────────────────────────

migrate:
    {{compose}} run --rm pmi-core migrate

# Load the fixture markets and register the YAML index definitions.
seed:
    {{compose}} run --rm pmi-core seed

score INDEX_ID='war-index-2026':
    {{compose}} run --rm pmi-core score {{INDEX_ID}}

# Fixture demo: migrate, load the synthetic markets, bind every factor to the
# fixture provider (pre-labelled judgements, no LLM), tag Senate races, then
# score the demo indices once a day for the last 14 days.
bootstrap: migrate seed
    {{compose}} run --rm pmi-core demo bind
    docker exec pmi-worker pmi-workers run tag-races
    {{compose}} run --rm pmi-core demo history --days 14

# Score an index definition in-process against the fixtures (no DB, no LLM).
dry-run YAML='pmi_core/index_defs/war-index-2026.yaml':
    cd {{root_dir}}/pmi-core && python -m pmi_core.cli dry-run {{YAML}} --compact

# ── tests (same commands CI runs) ────────────────────────────────────────────

# Throwaway pgvector container on :55432 for the DB-backed suites.
test-db:
    docker run -d --rm --name pmi-test-pg -e POSTGRES_USER=pmi -e POSTGRES_PASSWORD=pmi \
        -p 55432:5432 pgvector/pgvector:pg16

venv:
    uv venv --python 3.12 .venv
    uv pip install --python .venv -e "pmi-core[dev]" -e "pmi-notify[dev]" \
        -e "pmi-api[dev]" -e "pmi-workers[dev]" -e "pmi-mcp[dev]"

test PKG='pmi-core':
    cd {{root_dir}}/{{PKG}} && PMI_TEST_DB_HOST=localhost PMI_TEST_DB_PORT=55432 \
        PMI_MLFLOW_ENABLED=false PMI_API_REQUIRE_AUTH=false ../.venv/bin/pytest -q

test-all:
    for p in pmi-core pmi-notify pmi-api pmi-workers pmi-mcp; do just test $p || exit 1; done

lint:
    for p in pmi-core pmi-notify pmi-api pmi-workers pmi-mcp; do (cd $p && ../.venv/bin/ruff check .) || exit 1; done

web-check:
    cd {{root_dir}}/pmi-web && npm ci && npm run typecheck && npm test && npm run build

tf-check:
    cd {{root_dir}}/infra/terraform && terraform fmt -check -recursive && \
        terraform init -backend=false -input=false && terraform validate
