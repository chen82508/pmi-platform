"use client";

/**
 * Score history area chart.
 *
 *   - X-axis as numeric timestamp with scale="time"
 *   - Y-axis fitted to the data (padded, clamped to [0, 100]): a fixed 0–100
 *     axis flattens a two-week move of a couple of points into a straight line
 */

import { useId } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { fittedDomain } from "@/lib/chart";
import type { HistoryPoint } from "@/lib/types";

interface ScoreHistoryChartProps {
  points: HistoryPoint[];
  /** Pixel height for the responsive container. Default 320. */
  height?: number;
}

interface ChartPoint {
  ts: number;
  // null is a gap (index produced no score that tick); recharts skips
  // nulls when `connectNulls` is false, so the line breaks rather than
  // crashing on toFixed.
  score: number | null;
}

const BRAND = "#2563EB";
const AXIS = "#8A97AB";

export function ScoreHistoryChart({ points, height = 320 }: ScoreHistoryChartProps) {
  const gradientId = `g${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  if (points.length === 0) {
    return (
      <div className="flex items-center justify-center text-sm text-ink-muted" style={{ height }}>
        No history yet — run the pipeline a few times and refresh.
      </div>
    );
  }

  const data: ChartPoint[] = points.map((p) => ({
    ts: new Date(p.as_of).getTime(),
    score: p.score !== null ? Number(p.score) : null,
  }));
  const domain = fittedDomain(data.flatMap((d) => (d.score === null ? [] : [d.score])));

  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 12, right: 8, left: -8, bottom: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={BRAND} stopOpacity={0.22} />
              <stop offset="100%" stopColor={BRAND} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#E3E8EF" strokeDasharray="3 4" vertical={false} />
          <XAxis
            dataKey="ts"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            axisLine={false}
            tickLine={false}
            tickMargin={10}
            minTickGap={24}
            tick={{ fill: AXIS, fontSize: 12 }}
            tickFormatter={(ts: number) =>
              new Date(ts).toLocaleDateString("en-US", { month: "short", day: "numeric" })
            }
          />
          <YAxis
            domain={domain}
            tickCount={5}
            allowDecimals={false}
            axisLine={false}
            tickLine={false}
            width={44}
            tick={{ fill: AXIS, fontSize: 12 }}
          />
          <Tooltip
            cursor={{ stroke: BRAND, strokeOpacity: 0.35, strokeDasharray: "3 3" }}
            contentStyle={{
              background: "#0A2540",
              border: "none",
              borderRadius: 10,
              boxShadow: "0 8px 24px rgba(10,37,64,0.25)",
              padding: "8px 12px",
            }}
            labelStyle={{ color: "rgba(255,255,255,0.65)", fontSize: 12, marginBottom: 2 }}
            itemStyle={{ color: "#fff", fontWeight: 600, fontVariantNumeric: "tabular-nums" }}
            labelFormatter={(ts: number) =>
              new Date(ts).toLocaleString("en-US", {
                day: "numeric",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })
            }
            formatter={(v) => [typeof v === "number" ? v.toFixed(2) : "n/a", "Score"]}
          />
          <Area
            type="monotone"
            dataKey="score"
            stroke={BRAND}
            strokeWidth={2.25}
            fill={`url(#${gradientId})`}
            dot={false}
            activeDot={{ r: 4.5, fill: BRAND, stroke: "#fff", strokeWidth: 2 }}
            isAnimationActive={false}
            connectNulls={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
