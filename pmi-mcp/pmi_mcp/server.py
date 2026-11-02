"""pmi-mcp server — Tier A discovery tools over pmi-api (SHIP-2.3, first slice).

CLAUDE.md §8 rules applied: one action per tool, descriptions say WHEN to use
the tool, short ``required`` lists, ``summary`` + ``data`` envelopes, and
errors returned as ``{error: {code, hint}}`` with a next step instead of a
raised exception (the calling agent can read them and route around).

The tools are thin wrappers over pmi-api REST — the API stays the only gateway
to Postgres, so the MCP layer adds an agent surface without adding a second
data path. Responses are deliberately compacted (top-N slices, downsampled
series): the consumer is an LLM context window, not a UI.

Transport: streamable HTTP on ``0.0.0.0:${PMI_MCP_PORT:-8100}`` (path /mcp).
In-platform consumer: pmi-api's /ask agent (pmi_api/ask_agent.py). External
MCP clients (Claude / Cursor) reach the same endpoint wherever the port is
published.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

PMI_API_URL = os.environ.get("PMI_API_URL", "http://pmi-api:8000").rstrip("/")
# Same service key the pmi-web proxy uses (X-API-Key); harmless when auth is off.
PMI_API_KEY = os.environ.get("PMI_API_KEY", "")
PORT = int(os.environ.get("PMI_MCP_PORT", "8100"))

# stateless_http: each request is self-contained, so the per-/ask short-lived
# client sessions never accumulate server-side session state.
mcp = FastMCP("micah-pmi", host="0.0.0.0", port=PORT, stateless_http=True)


def _error(code: str, hint: str) -> dict:
    return {"error": {"code": code, "hint": hint}}


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    headers = {"X-API-Key": PMI_API_KEY} if PMI_API_KEY else {}
    try:
        async with httpx.AsyncClient(base_url=PMI_API_URL, timeout=60.0) as client:
            res = await client.get(path, params=params, headers=headers)
    except httpx.HTTPError as exc:
        return _error("PMI_API_UNREACHABLE", f"{type(exc).__name__} calling GET {path}")
    if res.status_code >= 400:
        # pmi-api 4xx bodies already carry {error:{code,hint}} under `detail`;
        # surface them to the agent rather than raising.
        try:
            body = res.json()
        except ValueError:
            return _error(f"HTTP_{res.status_code}", f"GET {path} failed")
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict) and "error" in detail:
            return detail
        return _error(f"HTTP_{res.status_code}", str(detail)[:200])
    return res.json()


def _is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and "error" in payload


@mcp.tool()
async def list_indexes() -> dict:
    """List every live PMI index (id, version, title). Call this first when you
    need valid index_id values for the other tools, or to survey what Micah
    tracks."""
    rows = await _get("/indexes")
    if _is_error(rows):
        return rows
    data = [{"id": r["id"], "version": r["version"], "title": r["title"]} for r in rows]
    return {"summary": f"{len(data)} live indexes", "data": data}


@mcp.tool()
async def get_index_score(index_id: str) -> dict:
    """Current reading of one index. Use when asked what an index says right
    now. Most indexes read 0-100 (higher = more conflict / more
    Republican-leaning / more bullish); *-republican-seats indexes read in
    expected seats instead. Get index_id values from list_indexes."""
    env = await _get(f"/indexes/{index_id}/score")
    if _is_error(env):
        return env
    d = env["data"]
    return {
        "summary": env.get("summary", ""),
        "data": {
            "index_id": d["index_id"],
            "version": d["version"],
            "score": d["score"],
            "as_of": d["as_of"],
            "component_count": d["component_count"],
            "market_count": d.get("market_count", 0),
            "venues": d.get("venues", []),
        },
    }


@mcp.tool()
async def get_index_history(index_id: str, days: int = 7) -> dict:
    """How an index moved over the last `days` days (default 7): first/last/
    min/max readings, net delta, and a downsampled series. Use for questions
    about change, trend, or 'what moved this week'."""
    days = max(1, min(days, 90))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    env = await _get(
        f"/indexes/{index_id}/score/history", {"from": since, "limit": 2000}
    )
    if _is_error(env):
        return env
    points = [p for p in env["data"]["points"] if p["score"] is not None]
    if not points:
        return {
            "summary": f"'{index_id}' has no scored points in the last {days}d",
            "data": {"index_id": index_id, "days": days, "points": []},
        }
    scores = [p["score"] for p in points]
    # Ceiling division keeps the series at ≤24 samples (+ the final point).
    step = max(1, -(-len(points) // 24))
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    first, last = points[0], points[-1]
    delta = round(last["score"] - first["score"], 2)
    return {
        "summary": (
            f"'{index_id}' moved {delta:+.2f} over {days}d "
            f"({first['score']:.2f} → {last['score']:.2f})"
        ),
        "data": {
            "index_id": index_id,
            "days": days,
            "n_points": len(points),
            "first": first,
            "last": last,
            "min": min(scores),
            "max": max(scores),
            "delta": delta,
            "points": [{"as_of": p["as_of"], "score": p["score"]} for p in sampled],
        },
    }


@mcp.tool()
async def explain_index(index_id: str, top: int = 8) -> dict:
    """Top contributing markets behind an index's current score — titles,
    venues, live prices and relevancy weights. Use for 'why is it at X' /
    'what's driving it' questions."""
    top = max(1, min(top, 15))
    payload = await _get(f"/indexes/{index_id}/explain")
    if _is_error(payload):
        return payload
    comps = payload.get("components", [])
    ranked = sorted(
        comps,
        key=lambda c: ((c.get("relevancy") or 0.0), (c.get("volume_24h") or 0.0)),
        reverse=True,
    )[:top]
    return {
        "summary": (
            f"'{index_id}' scored {payload['score']} from {len(comps)} components; "
            f"top {len(ranked)} attached"
        ),
        "data": {
            "index_id": payload["index_id"],
            "version": payload["version"],
            "as_of": payload["as_of"],
            "score": payload["score"],
            "n_components": len(comps),
            "top_components": [
                {
                    "title": c["title"],
                    "venue": c.get("venue"),
                    "last_price": c.get("last_price"),
                    "relevancy": c.get("relevancy"),
                    "direction": c.get("direction"),
                    "volume_24h": c.get("volume_24h"),
                }
                for c in ranked
            ],
        },
    }


@mcp.tool()
async def get_top_movers() -> dict:
    """Today's biggest 24h moves among the contracts inside Micah's published
    indexes. Use to add market-level colour to 'what changed' answers."""
    env = await _get("/landing/movers")
    if _is_error(env):
        return env
    rows = (env.get("data") or {}).get("movers", [])[:10]
    as_of = (env.get("data") or {}).get("as_of")
    return {
        "summary": f"{len(rows)} top movers as of {as_of}",
        "data": {
            "as_of": as_of,
            "movers": [
                {
                    "title": r.get("title"),
                    "index": r.get("tag"),
                    "delta_pts": r.get("delta_pts"),
                    "prob_pct": r.get("prob"),
                }
                for r in rows
            ],
        },
    }


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
