"use client";

/**
 * Score history line chart.
 *
 *   - recharts ResponsiveContainer + LineChart
 *   - X-axis as numeric timestamp with scale="time"
 *   - Y-axis fitted to the data (padded, clamped to [0, 100]): a fixed 0–100
 *     axis flattens a two-week move of a couple of points into a straight line
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fittedDomain } from "@/lib/chart";
import type { HistoryPoint } from "@/lib/types";

interface ScoreHistoryChartProps {
  points: HistoryPoint[];
  /** Pixel height for the responsive container. Default 360. */
  height?: number;
}

interface ChartPoint {
  ts: number;
  // null is a gap (index produced no score that tick); recharts skips
  // nulls when `connectNulls` is false, so the line breaks rather than
  // crashing on toFixed.
  score: number | null;
}

export function ScoreHistoryChart({ points, height = 360 }: ScoreHistoryChartProps) {
  if (points.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-sm text-ink-muted"
        style={{ height }}
      >
        No history yet — run the pipeline a few times and refresh.
      </div>
    );
  }

  const data: ChartPoint[] = points.map((p) => ({
    ts: new Date(p.as_of).getTime(),
    score: p.score !== null ? Number(p.score) : null,
  }));
  const domain = fittedDomain(
    data.flatMap((d) => (d.score === null ? [] : [d.score])),
  );

  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 20, right: 20, left: 0, bottom: 40 }}>
          <CartesianGrid stroke="#B9C0D4" vertical={false} />
          <XAxis
            dataKey="ts"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            stroke="#30374F"
            tick={{ fill: "#30374F", fontSize: 12 }}
            tickFormatter={(ts: number) => {
              const d = new Date(ts);
              const day = d.getDate();
              const month = d.toLocaleString("default", { month: "short" });
              return `${month} ${day}`;
            }}
          />
          <YAxis
            stroke="#30374F"
            domain={domain}
            tickCount={6}
            allowDecimals={false}
            tick={{ fill: "#30374F", fontSize: 12 }}
          />
          <Tooltip
            cursor={false}
            contentStyle={{
              background: "#F7B27A",
              border: "none",
              borderRadius: 8,
              color: "#30374F",
              fontWeight: 600,
            }}
            labelFormatter={(ts: number) =>
              new Date(ts).toLocaleString("en-US", {
                day: "numeric",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })
            }
            formatter={(v) => [
              typeof v === "number" ? v.toFixed(2) : "n/a",
              "score",
            ]}
          />
          <Line
            type="monotone"
            dataKey="score"
            stroke="#6594AB"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
