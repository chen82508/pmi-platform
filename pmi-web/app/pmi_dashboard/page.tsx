import Link from "next/link";

import { api, PmiApiError } from "@/lib/api-client";
import type { IndexSummary } from "@/lib/types";

export const dynamic = "force-dynamic";

async function loadIndexes(): Promise<{ rows: IndexSummary[]; error: string | null }> {
  try {
    const rows = await api.listIndexes();
    return { rows, error: null };
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

export default async function Page() {
  const { rows, error } = await loadIndexes();

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Indexes</h1>
        <p className="text-ink-muted text-sm mt-1">
          Every <code className="text-xs">is_current=true</code> PMI definition served by{" "}
          <code className="text-xs">pmi-api</code>.
        </p>
      </header>

      {error ? (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900">
          <p className="font-medium">Failed to load indexes</p>
          <p className="font-mono text-xs mt-1">{error}</p>
          <p className="mt-2 text-red-700">
            Is pmi-api up? Try <code>just api-up</code> then refresh.
          </p>
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border border-surface-border bg-surface p-6 text-sm text-ink-muted">
          <p>No indexes yet.</p>
          <p className="mt-2">
            Run <code className="font-mono">just pmi-bootstrap</code> to seed one.
          </p>
        </div>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2">
          {rows.map((row) => (
            <li key={`${row.id}-${row.version}`}>
              <Link
                href={`/pmi_dashboard/indexes/${encodeURIComponent(row.id)}`}
                className="block rounded-lg border border-surface-border bg-surface p-5 hover:border-accent transition-colors"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <h2 className="text-lg font-medium">{row.title}</h2>
                  <span className="text-xs text-ink-muted">v{row.version}</span>
                </div>
                <p className="text-xs font-mono text-ink-muted mt-1">{row.id}</p>
                <dl className="mt-3 text-xs text-ink-muted grid grid-cols-2 gap-y-1">
                  <dt>owner</dt>
                  <dd className="text-right">{row.owner ?? "—"}</dd>
                  <dt>yaml sha</dt>
                  <dd className="text-right font-mono">{row.yaml_sha256.slice(0, 10)}…</dd>
                  <dt>effective</dt>
                  <dd className="text-right">
                    {new Date(row.effective_from).toISOString().slice(0, 10)}
                  </dd>
                </dl>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
