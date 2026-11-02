import { api, PmiApiError } from "@/lib/api-client";
import type { SourceHealthRow } from "@/lib/types";

export const dynamic = "force-dynamic";

interface State {
  apiHealth: { status: string; db: string } | null;
  sources: SourceHealthRow[];
  error: string | null;
}

async function load(): Promise<State> {
  try {
    const [apiHealth, sources] = await Promise.all([
      api.health(),
      api.sourcesHealth().catch((e) => {
        // /sources/health may not exist yet — surface but don't crash.
        if (e instanceof PmiApiError && e.status === 404) return [];
        throw e;
      }),
    ]);
    return { apiHealth, sources, error: null };
  } catch (e) {
    const message =
      e instanceof PmiApiError
        ? `${e.status} from pmi-api at ${e.url}`
        : e instanceof Error
          ? e.message
          : String(e);
    return { apiHealth: null, sources: [], error: message };
  }
}

const STATUS_COLORS: Record<string, string> = {
  healthy: "bg-emerald-100 text-emerald-900 border-emerald-300",
  degraded: "bg-amber-100 text-amber-900 border-amber-300",
  down: "bg-red-100 text-red-900 border-red-300",
};

export default async function Health() {
  const { apiHealth, sources, error } = await load();

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Health</h1>
        <p className="text-ink-muted text-sm mt-1">
          Operational status from <code className="font-mono text-xs">pmi-api</code>.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900 font-mono">
          {error}
        </div>
      )}

      <section className="rounded-lg border border-surface-border bg-surface p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
          pmi-api
        </h2>
        <dl className="mt-3 grid grid-cols-[120px_1fr] text-sm gap-y-1">
          <dt className="text-ink-muted">status</dt>
          <dd>{apiHealth?.status ?? "—"}</dd>
          <dt className="text-ink-muted">db</dt>
          <dd>{apiHealth?.db ?? "—"}</dd>
        </dl>
      </section>

      <section>
        <h2 className="text-sm font-medium uppercase tracking-wide text-ink-muted mb-3">
          Ingest sources
        </h2>
        {sources.length === 0 ? (
          <p className="text-sm text-ink-muted">No source rows yet.</p>
        ) : (
          <table className="w-full text-sm bg-surface border border-surface-border rounded-lg overflow-hidden">
            <thead className="text-xs uppercase tracking-wide text-ink-muted bg-surface-muted">
              <tr>
                <th className="text-left px-4 py-2 font-medium">source</th>
                <th className="text-left px-4 py-2 font-medium">status</th>
                <th className="text-right px-4 py-2 font-medium">last success</th>
                <th className="text-right px-4 py-2 font-medium">records 24h</th>
                <th className="text-right px-4 py-2 font-medium">failures</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.source} className="border-t border-surface-border">
                  <td className="px-4 py-2 font-mono text-xs">{s.source}</td>
                  <td className="px-4 py-2">
                    <span
                      className={`inline-block rounded-full border px-2 py-0.5 text-xs ${
                        STATUS_COLORS[s.status] ?? "bg-surface-muted text-ink-muted"
                      }`}
                    >
                      {s.status}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right text-xs text-ink-muted">
                    {s.last_success_at
                      ? new Date(s.last_success_at).toISOString().slice(0, 19).replace("T", " ")
                      : "—"}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {s.records_24h ?? "—"}
                    {s.expected_records_24h && (
                      <span className="text-ink-muted text-xs"> / {s.expected_records_24h}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {s.consecutive_failures}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
