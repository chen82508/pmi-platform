/**
 * Thin typed fetch wrapper over pmi-api.
 *
 * Two URL contexts:
 *   - Server (SSR / Server Components): `PMI_API_URL` (internal compose hostname).
 *   - Browser: `NEXT_PUBLIC_PMI_API_URL` (must be reachable from the user's host).
 *
 * The `serverFetch` helper picks the right base depending on where it runs,
 * which means Server Components can talk to pmi-api inside the docker network
 * while client-side `useEffect` calls go over localhost. Server-side calls
 * attach the service key; the browser path never sees it.
 */

import type {
  ExplainPayload,
  HistoryEnvelope,
  IndexSummary,
  ScoreEnvelope,
  SourceHealthRow,
} from "./types";

const SERVER_BASE =
  process.env.PMI_API_URL || process.env.NEXT_PUBLIC_PMI_API_URL || "http://pmi-api:8000";

const BROWSER_BASE = process.env.NEXT_PUBLIC_PMI_API_URL || "http://localhost:8001";

// Server-only service key. When pmi-api runs with PMI_API_REQUIRE_AUTH=true,
// every /indexes/* + /sources/health route needs `X-API-Key`. We attach it
// ONLY on the server (RSC / SSR) fetch path so the dashboard can read through
// a single service key minted via `pmi-core keys create`. It is read from a
// non-`NEXT_PUBLIC_` env var so Next never inlines it into client bundles.
const SERVER_API_KEY = process.env.PMI_API_KEY || "";

function isServer(): boolean {
  return typeof window === "undefined";
}

function pickBase(): string {
  return isServer() ? SERVER_BASE : BROWSER_BASE;
}

class PmiApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly url: string,
    public readonly body: unknown,
  ) {
    super(`pmi-api ${status} at ${url}`);
    this.name = "PmiApiError";
  }
}

async function get<T>(path: string, init?: RequestInit, baseOverride?: string): Promise<T> {
  const base = baseOverride ?? pickBase();
  const url = `${base}${path}`;
  // Default to no caching on the server so a Server Component refresh
  // surfaces a fresh score immediately. A caller that passes
  // `next.revalidate` opts into Next's data cache instead (and must not also
  // send `cache: "no-store"`, which would win).
  const cacheDefault: RequestInit =
    init?.cache || init?.next?.revalidate !== undefined ? {} : { cache: "no-store" };
  const res = await fetch(url, {
    ...cacheDefault,
    ...init,
    headers: {
      Accept: "application/json",
      // Inject the service key only on the server; never on the browser path
      // (would both leak the key and fail CORS preflight needlessly).
      ...(isServer() && SERVER_API_KEY ? { "X-API-Key": SERVER_API_KEY } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // ignore — body might not be JSON
    }
    throw new PmiApiError(res.status, url, body);
  }
  return (await res.json()) as T;
}

const revalidateFor = (seconds?: number): RequestInit | undefined =>
  seconds === undefined ? undefined : { next: { revalidate: seconds } };

export const api = {
  health: () => get<{ status: string; db: string }>("/health"),
  listIndexes: () => get<IndexSummary[]>("/indexes"),
  getIndex: (id: string) => get<IndexSummary>(`/indexes/${encodeURIComponent(id)}`),
  getScore: (id: string, opts?: { asOf?: string; revalidate?: number }) => {
    const qs = opts?.asOf ? `?as_of=${encodeURIComponent(opts.asOf)}` : "";
    return get<ScoreEnvelope>(`/indexes/${encodeURIComponent(id)}/score${qs}`, revalidateFor(opts?.revalidate));
  },
  getHistory: (id: string, opts?: { from?: string; to?: string; limit?: number; revalidate?: number }) => {
    const params = new URLSearchParams();
    if (opts?.from) params.set("from", opts.from);
    if (opts?.to) params.set("to", opts.to);
    if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
    const qs = params.toString() ? `?${params.toString()}` : "";
    return get<HistoryEnvelope>(`/indexes/${encodeURIComponent(id)}/score/history${qs}`, revalidateFor(opts?.revalidate));
  },
  explain: (id: string) => get<ExplainPayload>(`/indexes/${encodeURIComponent(id)}/explain`),
  sourcesHealth: () => get<SourceHealthRow[]>("/sources/health"),
};

export { PmiApiError };
