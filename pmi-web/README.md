# pmi-web

The operational dashboard, built with Next.js 15 (App Router) and Tailwind.

| Route | Shows |
|---|---|
| `/pmi_dashboard` | Every current index definition (version, owner, YAML sha) |
| `/pmi_dashboard/indexes/[id]` | Latest score, score history chart, on-demand refresh (polls the queued job), and a backtest panel with CSV export |
| `/pmi_dashboard/health` | API and database health, plus per-source ingest status |

Server components call `pmi-api` directly over the compose network using a
server-only `PMI_API_KEY`. Client components go through the thin
`/api/pmi/*` route handlers, so the key never reaches the browser.

```bash
npm ci
npm run dev          # :3000, expects pmi-api at PMI_API_URL
npm run typecheck && npm test && npm run build
```

The production image is a multi-stage build that ships the Next.js
`standalone` output and runs as a non-root user.
