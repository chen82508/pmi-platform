# pmi-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes the indices to AI agents such as Claude, Cursor or the platform's own
`/ask` endpoint. It uses FastMCP over streamable HTTP on `:8100/mcp` and is
stateless.

| Tool | Use |
|---|---|
| `list_indexes` | Discover valid index ids |
| `get_index_score` | Current reading |
| `get_index_history` | Movement over N days, downsampled to at most 25 points |
| `explain_index` | Top contributing markets behind the score |
| `get_top_movers` | Largest 24-hour moves among index components |

Design rules:
- One action per tool.
- Descriptions say *when* to use the tool.
- Every response is a `summary` plus a compact `data` payload sized for a
  context window.
- Errors come back as `{"error": {"code", "hint"}}` with a next step, instead
  of an exception.

The server is a thin client of `pmi-api`, which remains the only path to the
database.

```bash
PMI_API_URL=http://localhost:8001 pmi-mcp
../.venv/bin/pytest -q
```
