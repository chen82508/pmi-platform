import { formatAsOf } from "@/lib/text";
import type { IndexSummary, ScoreEnvelope } from "@/lib/types";

interface ScoreCardProps {
  envelope: ScoreEnvelope | null;
  meta: IndexSummary;
}

export function ScoreCard({ envelope, meta }: ScoreCardProps) {
  if (!envelope) {
    return (
      <div className="rounded-lg border border-amber-300 bg-amber-50 p-5 text-sm text-amber-900">
        <p className="font-medium">No score yet for {meta.title}.</p>
        <p className="mt-1">
          Run <code className="font-mono">just workers-score {meta.id}</code> (or wait for the
          next cron tick).
        </p>
      </div>
    );
  }

  const { data, summary } = envelope;
  const asOf = new Date(data.as_of);
  const computedAt = new Date(data.computed_at);

  return (
    <section className="rounded-lg border border-surface-border bg-surface p-6">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-wide text-ink-muted">Current score</p>
          <p className="mt-1 text-5xl font-semibold tabular-nums text-accent">
            {data.score !== null ? data.score.toFixed(2) : "—"}
          </p>
        </div>
        <div className="text-right text-xs text-ink-muted space-y-0.5">
          <p>
            as of <time dateTime={data.as_of}>{formatAsOf(asOf)}</time>
          </p>
          <p>{data.component_count} components</p>
          <p>computed {computedAt.toISOString().slice(0, 19).replace("T", " ")} UTC</p>
        </div>
      </div>
      <p className="mt-3 text-sm text-ink-muted">{summary}</p>
    </section>
  );
}
