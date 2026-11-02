import type { HistoryPoint } from "@/lib/types";

/** Seat indices read in seats; everything else on a 0–100 scale. */
export function unitOf(indexId: string): string {
  return indexId.endsWith("-seats") ? "seats" : "/ 100";
}

/** First-to-last change over the scored points, and the span in whole days. */
export function seriesChange(points: HistoryPoint[]): { delta: number; days: number } | null {
  const scored = points.filter((p) => p.score !== null);
  if (scored.length < 2) return null;
  const first = scored[0];
  const last = scored[scored.length - 1];
  return {
    delta: Number(last.score) - Number(first.score),
    days: Math.round((new Date(last.as_of).getTime() - new Date(first.as_of).getTime()) / 86_400_000),
  };
}
