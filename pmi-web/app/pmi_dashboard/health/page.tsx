import { connection } from "next/server";

import { api, PmiApiError } from "@/lib/api-client";
import { SNAPSHOT_AT, STATIC_EXPORT } from "@/lib/static-export";
import { formatAsOf } from "@/lib/text";
import type { SourceHealthRow } from "@/lib/types";

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

const STATUS_STYLES: Record<string, string> = {
  healthy: "bg-positive-soft text-[#047857]",
  degraded: "bg-[#FEF3C7] text-[#92400E]",
  down: "bg-negative-soft text-[#B91C1C]",
};

function Pill({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ${
        ok ? "bg-positive/15 text-positive" : "bg-negative/15 text-[#FCA5A5]"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-positive" : "bg-negative"}`} />
      {label}
    </span>
  );
}

// Health read at build time would be stale the moment it was published, so
// the static export says what it is instead of showing old green pills.
function SnapshotHealth() {
  return (
    <>
      <section className="hero-band pb-20 pt-10">
        <div className="container-x">
          <p className="eyebrow text-[#93B4F8]">Operations</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Health</h1>
        </div>
      </section>
      <div className="container-x -mt-10">
        <section className="card p-5 text-sm">
          <h2 className="card-title">Static snapshot</h2>
          <p className="mt-2 max-w-2xl text-ink-muted">
            This copy of the dashboard was rendered from the fixture demo on{" "}
            {formatAsOf(SNAPSHOT_AT)} and has no API or database behind it, so there is no
            live health to report. Run <code className="font-mono">just e2e</code> to start the
            full stack with this page reading pmi-api.
          </p>
        </section>
      </div>
    </>
  );
}

export default async function Health() {
  if (STATIC_EXPORT) return <SnapshotHealth />;
  // See the index list page: the live build renders per request.
  await connection();
  const { apiHealth, sources, error } = await load();
  const apiOk = apiHealth?.status === "ok";
  const dbOk = String(apiHealth?.db) === "true" || apiHealth?.db === "ok";

  return (
    <>
      <section className="hero-band pb-20 pt-10">
        <div className="container-x">
          <p className="eyebrow text-[#93B4F8]">Operations</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">Health</h1>
          <div className="mt-6 flex flex-wrap gap-3">
            <Pill ok={apiOk} label={`pmi-api ${apiHealth?.status ?? "unreachable"}`} />
            <Pill ok={dbOk} label={dbOk ? "database connected" : "database unavailable"} />
          </div>
        </div>
      </section>

      <div className="container-x -mt-10 space-y-6">
        {error && (
          <div className="card p-5 font-mono text-sm text-[#B91C1C]">{error}</div>
        )}

        <section className="card p-5">
          <div className="mb-4 flex items-baseline justify-between">
            <h2 className="card-title">Ingest sources</h2>
            <span className="text-xs text-ink-muted">audit_source_health</span>
          </div>
          {sources.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No source rows. Exchange ingestion is not part of this repository; the demo data
              is seeded from fixtures.
            </p>
          ) : (
            <div className="-mx-5 -mb-5 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-y border-surface-border bg-surface-muted text-ink-muted">
                    <th className="eyebrow px-5 py-2.5 text-left">Source</th>
                    <th className="eyebrow px-4 py-2.5 text-left">Status</th>
                    <th className="eyebrow px-4 py-2.5 text-right">Last success</th>
                    <th className="eyebrow px-4 py-2.5 text-right">Records 24h</th>
                    <th className="eyebrow px-5 py-2.5 text-right">Failures</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border">
                  {sources.map((s) => (
                    <tr key={s.source}>
                      <td className="px-5 py-3 font-mono text-xs">{s.source}</td>
                      <td className="px-4 py-3">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                            STATUS_STYLES[s.status] ?? "bg-surface-muted text-ink-muted"
                          }`}
                        >
                          {s.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right text-xs text-ink-muted">
                        {s.last_success_at
                          ? new Date(s.last_success_at).toISOString().slice(0, 19).replace("T", " ")
                          : "—"}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums">
                        {s.records_24h ?? "—"}
                        {s.expected_records_24h && (
                          <span className="text-xs text-ink-muted"> / {s.expected_records_24h}</span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right tabular-nums">{s.consecutive_failures}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
