/**
 * Container liveness probe. Answers from the Node process alone — no pmi-api
 * call, no DB — because Docker restarts an unhealthy container, and a web
 * server that is up should not be killed for an upstream's slowness.
 *
 * The Dockerfile's HEALTHCHECK used to fetch "/", which is the landing page:
 * force-dynamic, ~500KB, and six upstream requests deep, against a 3s timeout.
 * It failed every probe on a working site.
 */
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok" }, { headers: { "cache-control": "no-store" } });
}
