# pmi-api

FastAPI service over the platform database.

| Area | Routes |
|---|---|
| Indices | `GET /indexes`, `/indexes/{id}`, `/score`, `/score/history`, `/explain` |
| On demand | `GET /indexes/{id}/score?max_age_s=&wait_s=` serves the latest row or enqueues a tick (202 + job id); `POST /score/refresh`; `GET /jobs/{id}` |
| Backtests | `POST /indexes/{id}/backtest` → `GET /workflows/{id}` (`/csv`) |
| Boards | seat boards, markets board, and the computed verticals (`/war`, `/gdp`, `/climate`, …), served from `ts_page_cache` |
| Agents | `POST /ask`: answers questions by calling the [MCP server](../pmi-mcp/) tools and writing the answer with an LLM; per-account quotas |
| Ops | `GET /health`, `GET /sources/health` |

Auth is an `X-API-Key` header, checked against hashed keys in
`core_api_keys`. Set `PMI_API_REQUIRE_AUTH=false` for local development.
Unhandled exceptions return a structured 500 and are reported to Slack
through `pmi-notify`.

```bash
uvicorn pmi_api.main:app --reload       # or: docker compose --profile pmi up pmi-api
../.venv/bin/pytest -q                  # needs a Postgres (see root README)
```
