/**
 * POST /api/pmi/indexes/{id}/backtest?days=&step_hours=
 *   → pmi-api POST /indexes/{id}/backtest (202 + workflow_run_id)
 *
 * Durable replay over historical snapshots (CORR-8.1); the client polls
 * /api/pmi/workflows/{id} and pulls the CSV when it lands.
 */

import { proxyPmi } from "@/lib/pmi-proxy";

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const url = new URL(req.url);
  const qs = new URLSearchParams();
  for (const key of ["days", "step_hours"]) {
    const v = url.searchParams.get(key);
    if (v) qs.set(key, v);
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return proxyPmi(`/indexes/${encodeURIComponent(id)}/backtest${suffix}`, {
    method: "POST",
  });
}
