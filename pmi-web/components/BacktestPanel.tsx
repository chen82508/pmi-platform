"use client";

/**
 * Backtest panel: one-click replay to CSV from the dashboard. Kicks off
 * POST /api/pmi/indexes/{id}/backtest, polls the durable workflow's step
 * progress, then charts the replayed points and offers the raw CSV.
 *
 * The replay reads only historical ts_price_snapshots — nothing is written
 * to ts_index_scores — so running one is always safe.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ScoreHistoryChart } from "@/components/ScoreHistoryChart";
import type { HistoryPoint } from "@/lib/types";

interface WorkflowEnvelope {
  summary: string;
  data: {
    workflow_run_id: number;
    status: string; // queued | running | succeeded | failed | cancelled
    steps_done: number;
    steps_total: number | null;
    error?: string | null;
  };
}

type Phase = "idle" | "starting" | "running" | "done" | "error";

const POLL_MS = 2000;
const TIMEOUT_MS = 10 * 60_000; // long replays are legitimate — generous cap

/** Parse the workflow CSV (as_of,score,component_count) into chart points. */
function parseCsv(text: string): HistoryPoint[] {
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

export function BacktestPanel({ indexId }: { indexId: string }) {
  const [days, setDays] = useState(14);
  const [stepHours, setStepHours] = useState(24);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [points, setPoints] = useState<HistoryPoint[] | null>(null);
  const [csv, setCsv] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback(async () => {
    setPhase("starting");
    setError(null);
    setPoints(null);
    setCsv(null);
    setProgress(null);
    try {
      const res = await fetch(
        `/api/pmi/indexes/${encodeURIComponent(indexId)}/backtest?days=${days}&step_hours=${stepHours}`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(`backtest enqueue failed (${res.status})`);
      const wf = ((await res.json()) as WorkflowEnvelope).data;
      setPhase("running");

      const deadline = Date.now() + TIMEOUT_MS;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (!alive.current) return;
        const poll = await fetch(`/api/pmi/workflows/${wf.workflow_run_id}`);
        if (!poll.ok) continue; // transient — keep polling until the deadline
        const run = ((await poll.json()) as WorkflowEnvelope).data;
        setProgress(
          run.steps_total
            ? `${run.steps_done}/${run.steps_total} steps`
            : `${run.steps_done} steps`,
        );
        if (run.status === "succeeded") {
          const csvRes = await fetch(
            `/api/pmi/workflows/${wf.workflow_run_id}/csv?columns=as_of,score,component_count`,
          );
          if (!csvRes.ok) throw new Error(`CSV fetch failed (${csvRes.status})`);
          const text = await csvRes.text();
          if (!alive.current) return;
          setCsv(text);
          setPoints(parseCsv(text));
          setPhase("done");
          return;
        }
        if (run.status === "failed" || run.status === "cancelled") {
          throw new Error(run.error || `workflow ${wf.workflow_run_id} ${run.status}`);
        }
      }
      throw new Error("backtest still running after 10 min — check /workflows later");
    } catch (e) {
      if (!alive.current) return;
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [indexId, days, stepHours]);

  const download = useCallback(() => {
    if (!csv) return;
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${indexId}-backtest-${days}d.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [csv, indexId, days]);

  const busy = phase === "starting" || phase === "running";
  const inputCls =
    "w-20 rounded-md border border-surface-border bg-surface px-2 py-1 text-sm";

  return (
    <div className="rounded-lg border border-surface-border bg-surface p-4 space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <label className="text-sm">
          <span className="block text-xs text-ink-muted mb-1">Days back</span>
          <input
            type="number"
            min={1}
            max={730}
            value={days}
            disabled={busy}
            onChange={(e) => setDays(Math.max(1, Math.min(730, Number(e.target.value) || 1)))}
            className={inputCls}
          />
        </label>
        <label className="text-sm">
          <span className="block text-xs text-ink-muted mb-1">Step (hours)</span>
          <input
            type="number"
            min={1}
            max={168}
            value={stepHours}
            disabled={busy}
            onChange={(e) =>
              setStepHours(Math.max(1, Math.min(168, Number(e.target.value) || 1)))
            }
            className={inputCls}
          />
        </label>
        <button
          type="button"
          onClick={run}
          disabled={busy}
          className="rounded-md border border-surface-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-raised disabled:opacity-60 disabled:cursor-default"
        >
          {phase === "starting"
            ? "Starting…"
            : phase === "running"
              ? `Replaying… ${progress ?? ""}`
              : "Run backtest"}
        </button>
        {phase === "done" && csv && (
          <button
            type="button"
            onClick={download}
            className="rounded-md border border-surface-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-raised"
          >
            Download CSV
          </button>
        )}
      </div>

      {phase === "error" && error && (
        <p className="text-xs text-red-700 font-mono">{error}</p>
      )}

      {points && points.length > 0 && (
        <div>
          <p className="text-xs text-ink-muted mb-2">
            {points.filter((p) => p.score != null).length} scored of {points.length}{" "}
            replayed points (gaps = ticks below min_components or with no snapshot
            coverage).
          </p>
          <ScoreHistoryChart points={points} height={280} />
        </div>
      )}
      {points && points.length === 0 && (
        <p className="text-xs text-ink-muted">
          Workflow finished but returned no points — try a shorter window.
        </p>
      )}
    </div>
  );
}
