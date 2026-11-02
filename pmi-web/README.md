# pmi-web

The operational dashboard, built with Next.js 15 (App Router) and Tailwind.

| Route | Shows |
|---|---|
| `/pmi_dashboard` | Live indexes with score, 14-day change and sparkline, plus KPI totals; definitions without readings listed below |
| `/pmi_dashboard/indexes/[id]` | Score and change, history chart, the markets driving the score (`/explain`), on-demand refresh (polls the queued job), backtest with CSV export, definition lineage |
| `/pmi_dashboard/health` | API and database health, plus per-source ingest status |

Server components call `pmi-api` directly over the compose network using a
server-only `PMI_API_KEY`. Client components go through the thin
`/api/pmi/*` route handlers, so the key never reaches the browser.

```bash
npm ci
npm run dev          # :3000, expects pmi-api at PMI_API_URL
npm run typecheck && npm test && npm run build
```

## Design

A FinTech-style palette, defined once as Tailwind tokens in
[`tailwind.config.ts`](tailwind.config.ts) and shared component classes in
[`app/globals.css`](app/globals.css):

| Token | Use | HEX |
|---|---|---|
| `navy` | header and hero bands | `#0A2540` |
| `navy-2` | secondary dark | `#1D3A5F` |
| `canvas` | page background | `#F6F9FC` |
| `brand` | interactive elements and charts | `#2563EB` |
| `positive` | gains, healthy status | `#10B981` |
| `negative` | losses, failures | `#EF4444` |

Text on the light tinted pills uses darker shades of green and red so it stays
readable. Inter is self-hosted at build time through `next/font`, so the page
makes no request to Google Fonts at runtime.

The production image is a multi-stage build that ships the Next.js
`standalone` output and runs as a non-root user.
