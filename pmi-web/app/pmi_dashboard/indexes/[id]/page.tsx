import Link from "next/link";
import { notFound } from "next/navigation";

import { BacktestPanel } from "@/components/BacktestPanel";
import { DriversTable } from "@/components/DriversTable";
import { RefreshScoreButton } from "@/components/RefreshScoreButton";
import { ScoreCard } from "@/components/ScoreCard";
import { ScoreHistoryChart } from "@/components/ScoreHistoryChart";
import { api, PmiApiError } from "@/lib/api-client";
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

async function load(id: string): Promise<LoadResult> {
  try {
    const [meta, score, history, explain] = await Promise.all([
      api.getIndex(id),
      api.getScore(id).catch((e) => {
        // Score absent (NO_SCORE_YET) is a soft error — keep rendering the page.
        if (e instanceof PmiApiError && e.status === 404) return null;
        throw e;
      }),
      api.getHistory(id, { limit: 500 }).catch((e) => {
        if (e instanceof PmiApiError && e.status === 404) return null;
        throw e;
      }),
      // Explainability is optional for the page: any failure just hides the table.
      api.explain(id).catch(() => null),
    ]);
    return { meta, score, history, explain, error: null, notFound: false };
  } catch (e) {
    if (e instanceof PmiApiError && e.status === 404) {
      return {
        meta: null,
        score: null,
        history: null,
        explain: null,
        error: null,
        notFound: true,
      };
    }
    const message =
      e instanceof PmiApiError
        ? `${e.status} from pmi-api at ${e.url}`
        : e instanceof Error
          ? e.message
          : String(e);
    return {
      meta: null,
      score: null,
      history: null,
      explain: null,
      error: message,
      notFound: false,
    };
  }
}

export default async function IndexPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const decoded = decodeURIComponent(id);
  const { meta, score, history, explain, error, notFound: nf } = await load(decoded);

  if (nf || !meta) {
    if (error) {
      return (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900">
          <p className="font-medium">Failed to load {decoded}</p>
          <p className="font-mono text-xs mt-1">{error}</p>
        </div>
      );
    }
    notFound();
  }

  return (
    <div className="space-y-8">
      <header>
        <Link href="/pmi_dashboard" className="text-sm text-ink-muted hover:text-ink">
          ← back to all indexes
        </Link>
        <div className="mt-2 flex items-baseline justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{meta.title}</h1>
          <span className="flex items-center gap-3">
            <RefreshScoreButton indexId={meta.id} />
            <span className="text-xs text-ink-muted">v{meta.version}</span>
          </span>
        </div>
        <p className="text-xs font-mono text-ink-muted mt-1">{meta.id}</p>
      </header>

      <ScoreCard envelope={score} meta={meta} />

      <section>
        <h2 className="text-base font-medium mb-3">Score history</h2>
        <div className="rounded-lg border border-surface-border bg-surface p-4">
          <ScoreHistoryChart points={history?.data.points ?? []} />
        </div>
        {history && (
          <p className="text-xs text-ink-muted mt-2">{history.summary}</p>
        )}
      </section>

      {explain && explain.components.length > 0 && (
        <section>
          <h2 className="text-base font-medium mb-3">What drives the score</h2>
          <DriversTable components={explain.components} />
        </section>
      )}

      <section>
        <h2 className="text-base font-medium mb-3">Backtest</h2>
        <BacktestPanel indexId={meta.id} />
      </section>

      <section>
        <h2 className="text-base font-medium mb-3">Definition metadata</h2>
        <dl className="grid grid-cols-[200px_1fr] gap-y-2 text-sm bg-surface rounded-lg border border-surface-border p-5">
          <dt className="text-ink-muted">id</dt>
          <dd className="font-mono">{meta.id}</dd>
          <dt className="text-ink-muted">version</dt>
          <dd>{meta.version}</dd>
          <dt className="text-ink-muted">owner</dt>
          <dd>{meta.owner ?? "—"}</dd>
          <dt className="text-ink-muted">yaml sha256</dt>
          <dd className="font-mono text-xs break-all">{meta.yaml_sha256}</dd>
          <dt className="text-ink-muted">effective from</dt>
          <dd>{new Date(meta.effective_from).toISOString()}</dd>
        </dl>
      </section>
    </div>
  );
}
