import type { HistoryPoint } from "./types";

/** One precomputed replay shipped with the static export (see
 *  scripts/snapshot-backtests.mjs, which writes them). */
export interface BacktestRun {
  days: number;
  step_hours: number;
  csv: string;
}

/** Parse the workflow CSV (as_of,score,component_count[,...]) into chart
 *  points. Columns are found by header name, so extra columns are ignored; a
 *  CSV without an `as_of` column yields no points. An empty score cell is a
 *  tick below min_components and stays null (a gap), not 0. */
export function parseBacktestCsv(text: string): HistoryPoint[] {
  const lines = text.trim().split("\n");
  if (lines.length < 2) return [];
  const headers = lines[0].split(",");
  const col = (name: string) => headers.indexOf(name);
  const iAsOf = col("as_of");
  const iScore = col("score");
  const iCount = col("component_count");
  if (iAsOf < 0) return [];
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const rawScore = iScore >= 0 ? cells[iScore] : "";
    return {
      as_of: cells[iAsOf],
      score: rawScore === "" || rawScore == null ? null : Number(rawScore),
      component_count: iCount >= 0 ? Number(cells[iCount] || 0) : 0,
    };
  });
}
