import Link from "next/link";

import { Sparkline } from "@/components/Sparkline";
import { api, PmiApiError } from "@/lib/api-client";
import type { HistoryPoint, IndexSummary, ScorePayload } from "@/lib/types";

export const dynamic = "force-dynamic";

interface IndexRow {
  meta: IndexSummary;
  score: ScorePayload | null;
  points: HistoryPoint[];
}

// Seat indices read in seats, everything else on a 0–100 scale.
function unitOf(id: string): string {
  return id.endsWith("-seats") ? "seats" : "/ 100";
}

async function loadRow(meta: IndexSummary): Promise<IndexRow> {
  const soft404 = (e: unknown) => {
    if (e instanceof PmiApiError && e.status === 404) return null;
    throw e;
  };
  const [score, history] = await Promise.all([
    api.getScore(meta.id).catch(soft404),
    api.getHistory(meta.id, { limit: 500 }).catch(soft404),
  ]);
  return { meta, score: score?.data ?? null, points: history?.data.points ?? [] };
}

async function loadIndexes(): Promise<{ rows: IndexRow[]; error: string | null }> {
  try {
    const metas = await api.listIndexes();
    return { rows: await Promise.all(metas.map(loadRow)), error: null };
  } catch (e) {
    const message =
      e instanceof PmiApiError
        ? `${e.status} from pmi-api at ${e.url}`
        : e instanceof Error
          ? e.message
          : String(e);
    return { rows: [], error: message };
  }
}

function Delta({ points }: { points: HistoryPoint[] }) {
  const scored = points.filter((p) => p.score !== null);
  if (scored.length < 2) return null;
  const d = Number(scored[scored.length - 1].score) - Number(scored[0].score);
  const days = Math.round(
    (new Date(scored[scored.length - 1].as_of).getTime() - new Date(scored[0].as_of).getTime()) /
      86_400_000,
  );
  const tone = d > 0 ? "text-emerald-700" : d < 0 ? "text-red-700" : "text-ink-muted";
  return (
    <span className={`text-xs tabular-nums ${tone}`}>
      {d >= 0 ? "▲" : "▼"} {Math.abs(d).toFixed(2)} · {days}d
    </span>
  );
}

function ScoredCard({ meta, score, points }: IndexRow) {
  return (
    <Link
      href={`/pmi_dashboard/indexes/${encodeURIComponent(meta.id)}`}
      className="flex h-full flex-col rounded-lg border border-surface-border bg-surface p-5 hover:border-accent transition-colors"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-base font-medium leading-snug">{meta.title}</h2>
        <span className="text-xs text-ink-muted shrink-0">v{meta.version}</span>
      </div>
      <div className="mt-auto pt-3 flex items-end justify-between gap-4">
        <p className="text-3xl font-semibold tabular-nums text-accent">
          {score?.score?.toFixed(2)}
          <span className="ml-1 text-sm font-normal text-ink-muted">{unitOf(meta.id)}</span>
        </p>
        <Delta points={points} />
      </div>
      <div className="mt-2">
        <Sparkline points={points} />
      </div>
      <p className="mt-2 text-xs text-ink-muted">
        {score?.component_count} components · {score?.market_count} markets ·{" "}
        {score?.venues.join(", ")}
      </p>
    </Link>
  );
}

export default async function Page() {
  const { rows, error } = await loadIndexes();
  const scored = rows.filter((r) => r.score?.score != null);
  const idle = rows.filter((r) => r.score?.score == null);

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Indexes</h1>
        <p className="text-ink-muted text-sm mt-1">
          Every <code className="text-xs">is_current=true</code> PMI definition served by{" "}
          <code className="text-xs">pmi-api</code>, with its latest score and trend.
        </p>
      </header>

      {error ? (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900">
          <p className="font-medium">Failed to load indexes</p>
          <p className="font-mono text-xs mt-1">{error}</p>
          <p className="mt-2 text-red-700">
            Is pmi-api up? Try <code>just up</code> then refresh.
          </p>
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border border-surface-border bg-surface p-6 text-sm text-ink-muted">
          <p>No indexes yet.</p>
          <p className="mt-2">
            Run <code className="font-mono">just bootstrap</code> to load the fixture demo.
          </p>
        </div>
      ) : (
        <>
          <ul className="grid gap-4 md:grid-cols-3">
            {scored.map((row) => (
              <li key={`${row.meta.id}-${row.meta.version}`}>
                <ScoredCard {...row} />
              </li>
            ))}
          </ul>

          {idle.length > 0 && (
            <section>
              <h2 className="text-sm font-medium text-ink-muted mb-2">
                Defined, no readings in this environment
              </h2>
              <ul className="rounded-lg border border-surface-border bg-surface divide-y divide-surface-border text-sm">
                {idle.map(({ meta }) => (
                  <li key={meta.id}>
                    <Link
                      href={`/pmi_dashboard/indexes/${encodeURIComponent(meta.id)}`}
                      className="flex justify-between gap-4 px-4 py-2.5 hover:bg-surface-muted"
                    >
                      <span>{meta.title}</span>
                      <span className="font-mono text-xs text-ink-muted">
                        {meta.id} · v{meta.version}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
