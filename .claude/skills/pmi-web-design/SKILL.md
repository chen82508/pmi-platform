---
name: pmi-web-design
description: >-
  The pmi-web dashboard's FinTech design system and how to extend it: palette
  tokens, shared component classes, page anatomy (navy hero band with
  overlapping cards), charts, data-honesty rules, and how to regenerate the
  README screenshots and walkthrough GIF. Use for any change under pmi-web/
  (new page, new panel, restyle, chart change) and whenever README images need
  refreshing.
---

# pmi-web design

The dashboard lives under `/pmi_dashboard` (Next.js 15 App Router, Tailwind 3,
Recharts). Server components fetch through `lib/api-client.ts`; client
components call the `/api/pmi/*` route handlers so the API key never reaches the
browser. Keep that split.

### Two builds

The same code also builds a read-only static export (`NEXT_PUBLIC_PMI_STATIC=1`,
`just web-static`) that CI publishes to GitHub Pages. There is no API behind it
at runtime, so:

- Anything that calls `/api/pmi/*` from the browser (refresh, live backtest)
  is gated on `STATIC_EXPORT` from `lib/static-export.ts`, with an honest
  static substitute or nothing at all. Route handlers are `.ts` and only exist
  in the live build.
- Don't add `export const dynamic`: it must be a literal and breaks one of the
  two builds. Pages call `connection()` when not static; a dynamic route that
  needs `generateStaticParams` uses a `page.live.tsx` / `page.export.tsx` pair
  (see `app/pmi_dashboard/indexes/[id]/`).
- In the static build, an API error must throw (fail the build) instead of
  rendering the error card.

Verify both: `just web-check`, then `just web-static /pmi-platform` against a
running demo and look at `pmi-web/out` served under `/pmi-platform/`.

## 1. Palette — tokens only

Defined once in `tailwind.config.ts`; never write a raw hex in a component
unless it is listed under "derived" below.

| Token | HEX | Use |
|---|---|---|
| `navy` | `#0A2540` | header, hero bands, tooltip background |
| `navy-2` | `#1D3A5F` | secondary dark |
| `canvas` / `surface-muted` | `#F6F9FC` | page background, table headers, hover rows |
| `brand` | `#2563EB` | buttons, links, chart line, relevancy bars, focus rings |
| `positive` | `#10B981` | gains, healthy status, rising sparklines |
| `negative` | `#EF4444` | losses, failures, falling sparklines |
| `ink` / `ink-muted` / `ink-faint` | `#0A2540` / `#5B6B82` / `#8A97AB` | text hierarchy |
| `surface-border` | `#E3E8EF` | card borders, grid lines |

**Derived (allowed raw values):** `#047857` and `#B91C1C` are the text colours
on the light `positive-soft` / `negative-soft` pills, because the base green and
red are too light to read on a tinted background. On navy, use `text-positive`
and `#FCA5A5`. The hero eyebrow is `#93B4F8`.

Inter is loaded in `app/layout.tsx` through `next/font` (self-hosted at build).

## 2. Shared classes (`app/globals.css`)

| Class | Use |
|---|---|
| `container-x` | page width (`max-w-6xl px-6`) |
| `hero-band` | full-width navy band with the blue glow |
| `card`, `card-title` | white rounded card with border and `shadow-card`; its heading |
| `eyebrow` | uppercase micro-label (KPI labels, table headers) |
| `btn-primary` / `btn-secondary` / `btn-on-dark` | buttons on light or navy backgrounds |

Hover elevation: `hover:shadow-lift` with `hover:-translate-y-0.5`.

## 3. Page anatomy

```
header  (bg-navy, layout.tsx: logo · NavLinks · "Fixture data" badge)
section.hero-band  pb-24        ← eyebrow, title, KPIs / score
div.container-x  -mt-14         ← cards overlap the band
  section.card  (title + aside)  × N
footer
```

- The dashboard layout does not constrain width; each page renders its own
  hero and `container-x`. The detail page's `Section` helper is the card
  pattern to reuse.
- Numbers use `tabular-nums`. Units come from `lib/format.unitOf`; period
  change from `lib/format.seriesChange`, rendered by `components/DeltaPill`.
- Direction and status use pills (soft background and dark text), not bare
  coloured text.

## 4. Charts

- `ScoreHistoryChart`: area chart with a brand gradient, dashed horizontal
  grid, no axis lines, navy tooltip. The Y-axis uses `lib/chart.fittedDomain`
  (padded data range, clamped to 0–100): a fixed 0–100 axis flattens real
  moves into a line.
- `Sparkline`: same domain rule, green or red by first-to-last direction.
- SVG gradient ids come from `useId()` and are sanitized
  (`replace(/[^a-zA-Z0-9_-]/g, "")`); raw ids contain characters that break
  `url(#...)`.

## 5. Data honesty

- Show only what the API returns. No invented numbers, placeholder markets or
  demo-only constants in components.
- Indexes without a reading go in the compact "Defined, no readings" list, not
  as empty cards.
- Keep the "Fixture data" badge while the repo runs on fixtures.

## 6. Verify

```bash
just web-check                                                   # tsc, vitest, next build
docker compose --profile pmi --profile web up -d --build pmi-web # production image
```

Then look at `/pmi_dashboard`, an index page, and `/pmi_dashboard/health` in a
browser. Pure helpers in `lib/` get vitest tests next to them.

## 7. README screenshots and GIF

```bash
just e2e            # fresh, deterministic demo data
just screenshots    # writes docs/images/*.png and walkthrough.gif
```

- Run it right after `just e2e`: the hourly cron beat scores every definition
  at the top of the hour, which adds cards the README doesn't describe.
- `scripts/screenshots/capture.mjs` drives the installed Google Chrome through
  `playwright-core`. Stills are 2× at 1280 px wide, cropped to whole cards, and
  the GIF frames carry their own durations. `make_gif.py` (Pillow via `uv run`)
  scales to 960 px with a 256-colour palette; keep the GIF under ~4 MB.
- If a page's structure changes, update the selectors in `capture.mjs` (they
  locate cards by their `h2` text), then check the stills and a few frames
  before committing.
