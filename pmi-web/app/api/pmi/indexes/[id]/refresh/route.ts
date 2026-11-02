/**
 * POST /api/pmi/indexes/{id}/refresh → pmi-api POST /indexes/{id}/score/refresh
 *
 * §3.2 on-demand path for the browser: force-enqueue a pipeline tick (deduped
 * server-side) and hand back the 202 + job envelope for the client to poll
 * via /api/pmi/jobs/{job_id}.
 */

import { proxyPmi } from "@/lib/pmi-proxy";

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return proxyPmi(`/indexes/${encodeURIComponent(id)}/score/refresh`, {
    method: "POST",
  });
}
