import Link from "next/link";
import { connection } from "next/server";

import { DeltaPill } from "@/components/DeltaPill";
import { Sparkline } from "@/components/Sparkline";
import { api, PmiApiError } from "@/lib/api-client";
import { unitOf } from "@/lib/format";
import { STATIC_EXPORT } from "@/lib/static-export";
import { formatAsOf } from "@/lib/text";
import type { HistoryPoint, IndexSummary, ScorePayload } from "@/lib/types";

interface IndexRow {
  meta: IndexSummary;
  score: ScorePayload | null;
  points: HistoryPoint[];
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
    // A static export must fail the build, not publish the error card.
    if (STATIC_EXPORT) throw e;
    const message =
      e instanceof PmiApiError
        ? `${e.status} from pmi-api at ${e.url}`
        : e instanceof Error
          ? e.message
          : String(e);
    return { rows: [], error: message };
  }
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="eyebrow text-white/50">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">{value}</p>
    </div>
  );
}

function ScoredCard({ meta, score, points }: IndexRow) {
  return (
    <Link
      href={`/pmi_dashboard/indexes/${encodeURIComponent(meta.id)}`}
      className="card group flex h-full flex-col p-5 transition-all hover:-translate-y-0.5 hover:shadow-lift"
    >
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-[15px] font-semibold leading-snug text-ink group-hover:text-brand">
          {meta.title}
        </h2>
        <span className="shrink-0 rounded-md bg-surface-muted px-1.5 py-0.5 font-mono text-[11px] text-ink-muted">
          v{meta.version}
        </span>
      </div>
      <div className="mt-auto flex items-end justify-between gap-3 pt-4">
        <p className="text-[32px] font-semibold leading-none tracking-tight tabular-nums text-ink">
          {score?.score?.toFixed(2)}
          <span className="ml-1.5 text-sm font-medium text-ink-faint">{unitOf(meta.id)}</span>
        </p>
        <DeltaPill points={points} />
      </div>
      <div className="mt-3 -mx-1">
        <Sparkline points={points} />
      </div>
      <div className="mt-3 flex items-center justify-between border-t border-surface-border pt-3 text-xs text-ink-muted">
        <span>
          {score?.component_count} components · {score?.market_count} markets
        </span>
        <span className="capitalize">{score?.venues.join(" · ")}</span>
      </div>
    </Link>
  );
}

export default async function Page() {
  // Segment config (`dynamic`) must be a literal, so the live build opts into
  // per-request rendering here; the static export renders once at build.
  if (!STATIC_EXPORT) await connection();
  const { rows, error } = await loadIndexes();
  const scored = rows.filter((r) => r.score?.score != null);
  const idle = rows.filter((r) => r.score?.score == null);
  const markets = scored.reduce((n, r) => n + (r.score?.market_count ?? 0), 0);
  const venues = new Set(scored.flatMap((r) => r.score?.venues ?? []));
  const latest = scored
    .map((r) => r.score!.as_of)
    .sort()
    .at(-1);

  return (
    <>
      <section className="hero-band pb-24 pt-10">
        <div className="container-x">
          <p className="eyebrow text-[#93B4F8]">Prediction Market Indices</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Indexes</h1>
          <p className="mt-2 max-w-2xl text-sm text-white/65">
            Each index is a versioned YAML definition. Markets are judged against its factors,
            weighted by liquidity and relevance, and folded into one number you can trace back
            to every contract behind it.
          </p>
          {scored.length > 0 && (
            <div className="mt-8 grid grid-cols-2 gap-6 sm:grid-cols-4">
              <Kpi label="Live indexes" value={`${scored.length} / ${rows.length}`} />
              <Kpi label="Markets in scores" value={String(markets)} />
              <Kpi label="Venues" value={String(venues.size)} />
              <Kpi label="Last update" value={latest ? formatAsOf(latest) : "—"} />
            </div>
          )}
        </div>
      </section>

      <div className="container-x -mt-14 space-y-10">
        {error ? (
          <div className="card border-negative/30 p-5 text-sm">
            <p className="font-semibold text-[#B91C1C]">Failed to load indexes</p>
            <p className="mt-1 font-mono text-xs text-ink-muted">{error}</p>
            <p className="mt-2 text-ink-muted">
              Is pmi-api up? Try <code className="font-mono">just up</code> then refresh.
            </p>
          </div>
        ) : rows.length === 0 ? (
          <div className="card p-6 text-sm text-ink-muted">
            <p>No indexes yet.</p>
            <p className="mt-2">
              Run <code className="font-mono">just bootstrap</code> to load the fixture demo.
            </p>
          </div>
        ) : (
          <>
            <ul className="grid gap-5 md:grid-cols-3">
              {scored.map((row) => (
                <li key={`${row.meta.id}-${row.meta.version}`}>
                  <ScoredCard {...row} />
                </li>
              ))}
            </ul>

            {idle.length > 0 && (
              <section>
                <div className="mb-3 flex items-baseline justify-between">
                  <h2 className="card-title">Defined, no readings in this environment</h2>
                  <span className="text-xs text-ink-muted">{idle.length} definitions</span>
                </div>
                <ul className="card divide-y divide-surface-border overflow-hidden text-sm">
                  {idle.map(({ meta }) => (
                    <li key={meta.id}>
                      <Link
                        href={`/pmi_dashboard/indexes/${encodeURIComponent(meta.id)}`}
                        className="flex items-center justify-between gap-4 px-5 py-3 hover:bg-surface-muted"
                      >
                        <span className="flex items-center gap-2.5">
                          <span className="h-1.5 w-1.5 rounded-full bg-ink-faint" />
                          {meta.title}
                        </span>
                        <span className="font-mono text-xs text-ink-faint">
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
    </>
  );
}
