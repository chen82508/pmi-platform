"use client";

/** Compact score trend for the index list cards. */

import { useId } from "react";
import { Area, AreaChart, ResponsiveContainer, YAxis } from "recharts";

import { fittedDomain } from "@/lib/chart";
import type { HistoryPoint } from "@/lib/types";

export function Sparkline({ points, height = 56 }: { points: HistoryPoint[]; height?: number }) {
  const gradientId = `g${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const data = points.flatMap((p) =>
    p.score === null ? [] : [{ ts: new Date(p.as_of).getTime(), score: Number(p.score) }],
  );
  if (data.length < 2) return <div style={{ height }} />;
  const color = data[data.length - 1].score >= data[0].score ? "#10B981" : "#EF4444";
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.25} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={fittedDomain(data.map((d) => d.score))} />
          <Area
            type="monotone"
            dataKey="score"
            stroke={color}
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
