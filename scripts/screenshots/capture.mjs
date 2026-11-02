// README stills (written to docs/images/) + timed frames for the walkthrough GIF.
// Usage: node capture.mjs <dashboard base URL> <images dir> <frames dir>
// Drives the system Chrome install (playwright-core, no bundled browser).
import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";

const [, , base, outDir, frames] = process.argv;
mkdirSync(frames, { recursive: true });
const b = await chromium.launch({ channel: "chrome" });

// Page-absolute box of the card <section> whose heading contains `text`.
const boxOf = (page, text) =>
  page
    .locator("section", { has: page.locator("h2", { hasText: text }) })
    .last()
    .evaluate((e) => {
      const r = e.getBoundingClientRect();
      return { top: r.top + window.scrollY, bottom: r.bottom + window.scrollY };
    });
const topOf = async (page, text) => (await boxOf(page, text)).top;

// ── stills ──────────────────────────────────────────────────────────────────
const still = await b.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 2 });
await still.goto(`${base}/pmi_dashboard`, { waitUntil: "networkidle" });
await still.screenshot({ path: `${outDir}/dashboard-indexes.png`, fullPage: true });

// Header + hero + everything down to (and including) the `untilCard` section.
async function detail(id, file, untilCard) {
  await still.goto(`${base}/pmi_dashboard/indexes/${id}`, { waitUntil: "networkidle" });
  await still.waitForTimeout(500);
  const { bottom } = await boxOf(still, untilCard);
  await still.screenshot({ path: `${outDir}/${file}`, fullPage: true, clip: { x: 0, y: 0, width: 1280, height: bottom + 14 } });
}
await detail("war-index-2026", "index-war.png", "What drives the score");
await detail("us-senate-2026-republican-seats", "index-senate.png", "Score history");

await still.goto(`${base}/pmi_dashboard/indexes/btc-bull-bear-index`, { waitUntil: "networkidle" });
await still.getByRole("button", { name: "Run backtest" }).click();
await still.getByRole("button", { name: "Download CSV" }).waitFor({ timeout: 120000 });
await still.waitForTimeout(500);
const bt = await boxOf(still, "Backtest");
await still.screenshot({ path: `${outDir}/backtest.png`, fullPage: true, clip: { x: 64, y: bt.top - 12, width: 1152, height: bt.bottom - bt.top + 24 } });

// ── GIF frames: [file, duration ms] ────────────────────────────────────────
const p = await b.newPage({ viewport: { width: 1280, height: 760 } });
const log = [];
let n = 0;
async function snap(ms) {
  const f = `${frames}/${String(n++).padStart(4, "0")}.png`;
  await p.screenshot({ path: f });
  log.push([f, ms]);
}
async function glide(x0, y0, x1, y1, steps, ms) {
  for (let i = 0; i <= steps; i++) {
    await p.mouse.move(x0 + ((x1 - x0) * i) / steps, y0 + ((y1 - y0) * i) / steps);
    await p.waitForTimeout(30);
    await snap(ms);
  }
}
async function scrollTo(y, steps = 8) {
  const start = await p.evaluate(() => window.scrollY);
  for (let i = 1; i <= steps; i++) {
    await p.evaluate((v) => window.scrollTo(0, v), start + ((y - start) * i) / steps);
    await snap(60);
  }
}

await p.goto(`${base}/pmi_dashboard`, { waitUntil: "networkidle" });
await snap(1800);
const war = p.locator("a", { hasText: "Global War Index 2026" }).first();
const wb = await war.boundingBox();
await glide(300, 500, wb.x + wb.width / 2, wb.y + 60, 10, 60);
await snap(500);
await war.click();
await p.waitForLoadState("networkidle");
await p.waitForTimeout(400);
await snap(1500);

// Sweep the tooltip across the history chart.
await scrollTo((await topOf(p, "Score history")) - 16);
const chart = await p.locator(".recharts-surface").first().boundingBox();
const yMid = chart.y + chart.height / 2;
await glide(chart.x + 80, yMid, chart.x + chart.width - 25, yMid, 22, 90);
await snap(700);

await scrollTo((await topOf(p, "What drives the score")) - 24);
await snap(2200);

await scrollTo((await topOf(p, "Backtest")) - 160);
const btn = p.getByRole("button", { name: "Run backtest" });
const bb = await btn.boundingBox();
await glide(640, 300, bb.x + bb.width / 2, bb.y + bb.height / 2, 8, 60);
await btn.click();
for (let i = 0; i < 60; i++) {
  await snap(250);
  if (await p.getByRole("button", { name: "Download CSV" }).isVisible()) break;
  await p.waitForTimeout(200);
}
await p.waitForTimeout(300);
await snap(2800);

writeFileSync(`${frames}/frames.json`, JSON.stringify(log));
await b.close();
console.log(`stills + ${log.length} frames`);
