/** GET /api/pmi/jobs/{jobId} → pmi-api GET /jobs/{job_id} (poll handle). */

import { proxyPmi } from "@/lib/pmi-proxy";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;
  return proxyPmi(`/jobs/${encodeURIComponent(jobId)}`);
}
