import { seriesChange } from "@/lib/format";
import type { HistoryPoint } from "@/lib/types";

/** Period change as a tinted pill: green up, red down. */
export function DeltaPill({ points, onDark = false }: { points: HistoryPoint[]; onDark?: boolean }) {
  const change = seriesChange(points);
  if (!change) return null;
  const { delta, days } = change;
  const up = delta >= 0;
  const tone = onDark
    ? up
      ? "bg-positive/15 text-positive"
      : "bg-negative/15 text-[#FCA5A5]"
    : up
      ? "bg-positive-soft text-[#047857]"
      : "bg-negative-soft text-[#B91C1C]";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold tabular-nums ${tone}`}>
      {up ? "▲" : "▼"} {Math.abs(delta).toFixed(2)}
      <span className="font-medium opacity-70">{days}d</span>
    </span>
  );
}
