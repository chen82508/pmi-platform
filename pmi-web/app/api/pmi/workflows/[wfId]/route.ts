/** GET /api/pmi/workflows/{wfId} → pmi-api GET /workflows/{id} (poll handle). */

import { proxyPmi } from "@/lib/pmi-proxy";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ wfId: string }> },
) {
  const { wfId } = await params;
  return proxyPmi(`/workflows/${encodeURIComponent(wfId)}`);
}
