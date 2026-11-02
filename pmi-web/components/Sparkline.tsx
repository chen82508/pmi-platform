"use client";

/** Compact score trend for the index list cards. */

import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";

import { fittedDomain } from "@/lib/chart";
import type { HistoryPoint } from "@/lib/types";

export function Sparkline({ points, height = 48 }: { points: HistoryPoint[]; height?: number }) {
  const data = points.flatMap((p) =>
    p.score === null ? [] : [{ ts: new Date(p.as_of).getTime(), score: Number(p.score) }],
  );
  if (data.length < 2) return <div style={{ height }} />;
  const rising = data[data.length - 1].score >= data[0].score;
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 4, right: 0, left: 0, bottom: 4 }}>
          <YAxis hide domain={fittedDomain(data.map((d) => d.score))} />
          <Line
            type="monotone"
            dataKey="score"
            stroke={rising ? "#2E8B6A" : "#B4533A"}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
