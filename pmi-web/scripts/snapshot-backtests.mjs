#!/usr/bin/env node
/**
 * Precompute backtest replays for the static export, which has no backend to
 * run them on demand. For every index with a non-null current score, runs each preset
 * through the same API path the live panel uses (POST /backtest → poll the
 * workflow → GET its CSV) and writes { [index_id]: [{ days, step_hours, csv }] }.
 *
 * Needs a demo stack with a worker on the `default` lane, where the API
 * enqueues backtest workflows.
 *
 *   node scripts/snapshot-backtests.mjs http://localhost:8001 .snapshot/backtests.json
 */
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

// The fixture world carries 14 daily price points, so longer windows or
// sub-daily steps would only repeat the same snapshots.
const PRESETS = [
  { days: 7, step_hours: 24 },
  { days: 14, step_hours: 24 },
];
const POLL_MS = 500;
const TIMEOUT_MS = 120_000;

const [base, out] = process.argv.slice(2);
if (!base || !out) {
  console.error("usage: snapshot-backtests.mjs <pmi-api base url> <out.json>");
  process.exit(2);
}

async function call(urlPath, init) {
  const res = await fetch(`${base}${urlPath}`, init);
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${urlPath} → ${res.status}`);
  return res;
}

async function replay(id, { days, step_hours }) {
  const qs = `days=${days}&step_hours=${step_hours}`;
  const started = await call(`/indexes/${encodeURIComponent(id)}/backtest?${qs}`, { method: "POST" });
  const runId = (await started.json()).data.workflow_run_id;
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const run = (await (await call(`/workflows/${runId}`)).json()).data;
    if (run.status === "succeeded") {
      const csv = await call(`/workflows/${runId}/csv?columns=as_of,score,component_count`);
      return { days, step_hours, csv: await csv.text() };
    }
    if (run.status === "failed" || run.status === "cancelled") {
      throw new Error(`${id} ${days}d backtest ${run.status}: ${run.error ?? ""}`);
    }
  }
  throw new Error(`${id} ${days}d backtest still running after ${TIMEOUT_MS / 1000}s — is a default-lane worker up?`);
}

const indexes = await (await call("/indexes")).json();
const snapshot = {};
for (const { id } of indexes) {
  const score = await fetch(`${base}/indexes/${encodeURIComponent(id)}/score`);
  if (score.status === 404) continue; // never scored
  if (!score.ok) throw new Error(`GET /indexes/${id}/score → ${score.status}`);
  // A null score is a tick below min_components: the dashboard lists the
  // index as having no readings, so there is nothing worth replaying.
  if ((await score.json()).data.score == null) continue;
  snapshot[id] = [];
  for (const preset of PRESETS) snapshot[id].push(await replay(id, preset));
  console.log(`${id}: ${PRESETS.map((p) => `${p.days}d`).join(", ")}`);
}
if (Object.keys(snapshot).length === 0) throw new Error("no index has a score; run the demo bootstrap first");

mkdirSync(path.dirname(out), { recursive: true });
writeFileSync(out, `${JSON.stringify(snapshot, null, 2)}\n`);
console.log(`wrote ${out}`);
