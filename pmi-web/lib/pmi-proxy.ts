/**
 * Server-only proxy helper for Next.js Route Handlers under /api/pmi/*.
 *
 * The browser can never talk to pmi-api directly when auth is on — the
 * service key lives in a server-only env var (see lib/api-client.ts). Client
 * components (refresh button, backtest panel) therefore call these thin
 * /api/pmi/* handlers, which re-issue the request against the internal
 * compose base with `X-API-Key` attached and stream the JSON (or CSV) back.
 */

import { NextResponse } from "next/server";

const SERVER_BASE =
  process.env.PMI_API_URL || process.env.NEXT_PUBLIC_PMI_API_URL || "http://pmi-api:8000";

const SERVER_API_KEY = process.env.PMI_API_KEY || "";

/** Forward a request to pmi-api and mirror its status + body back. */
export async function proxyPmi(
  path: string,
  init?: RequestInit & { asText?: boolean },
): Promise<NextResponse> {
  const url = `${SERVER_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      cache: "no-store",
      ...init,
      headers: {
        Accept: init?.asText ? "text/csv" : "application/json",
        ...(SERVER_API_KEY ? { "X-API-Key": SERVER_API_KEY } : {}),
        ...(init?.headers || {}),
      },
    });
  } catch {
    return NextResponse.json(
      { error: { code: "PMI_API_UNREACHABLE", hint: `is pmi-api up at ${SERVER_BASE}?` } },
      { status: 502 },
    );
  }
  if (init?.asText) {
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("Content-Type") ?? "text/plain" },
    });
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = { error: { code: "BAD_UPSTREAM_BODY" } };
  }
  return NextResponse.json(body, { status: res.status });
}
