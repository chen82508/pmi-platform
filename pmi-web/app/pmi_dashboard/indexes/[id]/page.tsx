import Link from "next/link";
import { notFound } from "next/navigation";

import { BacktestPanel } from "@/components/BacktestPanel";
import { DeltaPill } from "@/components/DeltaPill";
import { DriversTable } from "@/components/DriversTable";
import { RefreshScoreButton } from "@/components/RefreshScoreButton";
import { ScoreHistoryChart } from "@/components/ScoreHistoryChart";
import { api, PmiApiError } from "@/lib/api-client";
import { unitOf } from "@/lib/format";
import { formatAsOf } from "@/lib/text";
import type { ExplainPayload, HistoryEnvelope, IndexSummary, ScoreEnvelope } from "@/lib/types";

export const dynamic = "force-dynamic";

interface LoadResult {
  meta: IndexSummary | null;
  score: ScoreEnvelope | null;
  history: HistoryEnvelope | null;
  explain: ExplainPayload | null;
  error: string | null;
  notFound: boolean;
}

const EMPTY = { meta: null, score: null, history: null, explain: null };

async function load(id: string): Promise<LoadResult> {
  const soft404 = (e: unknown) => {
    // Score absent (NO_SCORE_YET) is a soft error — keep rendering the page.
    if (e instanceof PmiApiError && e.status === 404) return null;
    throw e;
  };
  try {
    const [meta, score, history, explain] = await Promise.all([
      api.getIndex(id),
      api.getScore(id).catch(soft404),
      api.getHistory(id, { limit: 500 }).catch(soft404),
      // Explainability is optional for the page: any failure just hides the table.
      api.explain(id).catch(() => null),
    ]);
    return { meta, score, history, explain, error: null, notFound: false };
  } catch (e) {
    if (e instanceof PmiApiError && e.status === 404) {
      return { ...EMPTY, error: null, notFound: true };
    }
    const message =
      e instanceof PmiApiError
        ? `${e.status} from pmi-api at ${e.url}`
        : e instanceof Error
          ? e.message
          : String(e);
    return { ...EMPTY, error: message, notFound: false };
  }
}

function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="card p-5">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 className="card-title">{title}</h2>
        {aside && <span className="text-xs text-ink-muted">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

export default async function IndexPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const decoded = decodeURIComponent(id);
  const { meta, score, history, explain, error, notFound: nf } = await load(decoded);

  if (nf || !meta) {
    if (error) {
      return (
        <div className="container-x pt-10">
          <div className="card p-5 text-sm">
            <p className="font-semibold text-[#B91C1C]">Failed to load {decoded}</p>
            <p className="mt-1 font-mono text-xs text-ink-muted">{error}</p>
          </div>
        </div>
      );
    }
    notFound();
  }

  const points = history?.data.points ?? [];
  const data = score?.data;

  return (
    <>
      <section className="hero-band pb-24 pt-8">
        <div className="container-x">
          <Link href="/pmi_dashboard" className="text-sm text-white/60 hover:text-white">
            ← All indexes
          </Link>
          <div className="mt-4 flex flex-wrap items-start justify-between gap-6">
            <div>
              <h1 className="text-3xl font-semibold tracking-tight">{meta.title}</h1>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-white/60">
                <span className="rounded-md bg-white/10 px-2 py-0.5 font-mono">{meta.id}</span>
                <span className="rounded-md bg-white/10 px-2 py-0.5 font-mono">v{meta.version}</span>
                {data && <span>{data.venues.join(" · ")}</span>}
              </div>
            </div>
            <RefreshScoreButton indexId={meta.id} />
          </div>

          {data && data.score !== null ? (
            <div className="mt-8 flex flex-wrap items-end gap-x-10 gap-y-4">
              <div>
                <p className="eyebrow text-white/50">Current score</p>
                <p className="mt-1 flex items-baseline gap-3">
                  <span className="text-5xl font-semibold tracking-tight tabular-nums">
                    {data.score.toFixed(2)}
                  </span>
                  <span className="text-base text-white/50">{unitOf(meta.id)}</span>
                  <DeltaPill points={points} onDark />
                </p>
              </div>
              <dl className="grid grid-cols-3 gap-8 text-sm">
                <div>
                  <dt className="eyebrow text-white/50">Components</dt>
                  <dd className="mt-1 text-lg font-semibold tabular-nums">{data.component_count}</dd>
                </div>
                <div>
                  <dt className="eyebrow text-white/50">Markets</dt>
                  <dd className="mt-1 text-lg font-semibold tabular-nums">{data.market_count}</dd>
                </div>
                <div>
                  <dt className="eyebrow text-white/50">As of</dt>
                  <dd className="mt-1 text-lg font-semibold">{formatAsOf(data.as_of)}</dd>
                </div>
              </dl>
            </div>
          ) : (
            <p className="mt-8 text-sm text-white/65">
              No score yet. Run <code className="font-mono">just score {meta.id}</code> or wait for
              the next scheduled tick.
            </p>
          )}
        </div>
      </section>

      <div className="container-x -mt-14 space-y-6">
        <Section title="Score history" aside={history?.summary}>
          <ScoreHistoryChart points={points} />
        </Section>

        {explain && explain.components.length > 0 && (
          <Section
            title="What drives the score"
            aside="Price · relevancy from the factor judgements · direction of a YES"
          >
            <DriversTable components={explain.components} />
          </Section>
        )}

        <Section title="Backtest" aside="Replays past prices as a durable workflow; nothing is written">
          <BacktestPanel indexId={meta.id} />
        </Section>

        <Section title="Definition lineage">
          <dl className="grid grid-cols-[160px_1fr] gap-y-2.5 text-sm">
            <dt className="text-ink-muted">Index id</dt>
            <dd className="font-mono text-[13px]">{meta.id}</dd>
            <dt className="text-ink-muted">Version</dt>
            <dd>{meta.version}</dd>
            <dt className="text-ink-muted">Owner</dt>
            <dd>{meta.owner ?? "—"}</dd>
            <dt className="text-ink-muted">YAML sha256</dt>
            <dd className="break-all font-mono text-xs text-ink-muted">{meta.yaml_sha256}</dd>
            <dt className="text-ink-muted">Effective from</dt>
            <dd>{formatAsOf(meta.effective_from)}</dd>
          </dl>
        </Section>
      </div>
    </>
  );
}
