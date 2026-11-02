"use client";

/**
 * On-demand score refresh (§3.2): POST /api/pmi/indexes/{id}/refresh, poll
 * /api/pmi/jobs/{job_id} until the worker lands the tick, then re-render the
 * surrounding Server Component tree via router.refresh() so the new score /
 * history / board read through without a full reload.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

interface JobEnvelope {
  summary: string;
  data: { job_id: number; status: string; error?: string | null };
}

type Phase = "idle" | "queueing" | "running" | "done" | "error";

const POLL_MS = 1500;
const TIMEOUT_MS = 120_000;

export function RefreshScoreButton({ indexId }: { indexId: string }) {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback(async () => {
    setPhase("queueing");
    setDetail(null);
    try {
      const res = await fetch(`/api/pmi/indexes/${encodeURIComponent(indexId)}/refresh`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`refresh enqueue failed (${res.status})`);
      const envelope = (await res.json()) as JobEnvelope;
      const jobId = envelope.data.job_id;
      setPhase("running");

      const deadline = Date.now() + TIMEOUT_MS;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (!alive.current) return;
        const poll = await fetch(`/api/pmi/jobs/${jobId}`);
        if (!poll.ok) continue; // transient — keep polling until the deadline
        const job = ((await poll.json()) as JobEnvelope).data;
        if (job.status === "succeeded") {
          setPhase("done");
          router.refresh();
          setTimeout(() => alive.current && setPhase("idle"), 2500);
          return;
        }
        if (job.status === "failed") {
          throw new Error(job.error || `job ${jobId} failed`);
        }
      }
      throw new Error(`still running after ${TIMEOUT_MS / 1000}s — check /jobs later`);
    } catch (e) {
      if (!alive.current) return;
      setPhase("error");
      setDetail(e instanceof Error ? e.message : String(e));
    }
  }, [indexId, router]);

  const busy = phase === "queueing" || phase === "running";
  const label =
    phase === "queueing"
      ? "Queueing…"
      : phase === "running"
        ? "Recomputing…"
        : phase === "done"
          ? "Updated ✓"
          : phase === "error"
            ? "Retry refresh"
            : "Refresh score";


  return (
    <span className="inline-flex items-center gap-2">
      <button type="button" className="btn-on-dark" onClick={run} disabled={busy}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={busy ? "animate-spin" : undefined}>
          <path d="M21 12a9 9 0 1 1-2.64-6.36" />
          <path d="M21 3v6h-6" />
        </svg>
        {label}
      </button>
      {phase === "error" && detail && (
        <span className="text-xs text-[#FCA5A5]">{detail}</span>
      )}
    </span>
  );
}
