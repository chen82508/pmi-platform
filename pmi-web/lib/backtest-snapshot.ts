import { readFile } from "node:fs/promises";
import path from "node:path";

import type { BacktestRun } from "./backtest";

/** Where scripts/snapshot-backtests.mjs leaves the precomputed replays. */
export const SNAPSHOT_FILE = path.join(process.cwd(), ".snapshot", "backtests.json");

let cache: Promise<Record<string, BacktestRun[]>> | null = null;

/** Replays for one index, read at build time by the static export. A missing
 *  file fails the build instead of publishing a site whose backtest panels are
 *  silently empty. */
export async function snapshotBacktests(indexId: string): Promise<BacktestRun[]> {
  cache ??= readFile(SNAPSHOT_FILE, "utf8").then(
    (text) => JSON.parse(text) as Record<string, BacktestRun[]>,
    (e: unknown) => {
      throw new Error(
        `static export needs ${SNAPSHOT_FILE}; run scripts/snapshot-backtests.mjs first (${String(e)})`,
      );
    },
  );
  return (await cache)[indexId] ?? [];
}
