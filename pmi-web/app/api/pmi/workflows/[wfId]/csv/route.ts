/** GET /api/pmi/workflows/{wfId}/csv → pmi-api CSV passthrough (text). */

import { proxyPmi } from "@/lib/pmi-proxy";

export async function GET(
  req: Request,
  { params }: { params: Promise<{ wfId: string }> },
) {
  const { wfId } = await params;
  const url = new URL(req.url);
  const columns = url.searchParams.get("columns");
  const suffix = columns ? `?columns=${encodeURIComponent(columns)}` : "";
  return proxyPmi(`/workflows/${encodeURIComponent(wfId)}/csv${suffix}`, {
    asText: true,
  });
}
