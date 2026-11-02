"""Tool-level tests for pmi-mcp: the pmi-api HTTP layer is replaced with an
httpx MockTransport, so the tests exercise envelope shaping and error routing
without a running API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pmi_mcp import server

pytestmark = pytest.mark.asyncio


def _route(responses: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        return responses.get(request.url.path, httpx.Response(404, json={"detail": "nope"}))

    return handler


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    """Install a fake pmi-api; call the returned function with path → response."""
    real_client = httpx.AsyncClient

    def install(responses: dict[str, httpx.Response]) -> None:
        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(_route(responses))
            return real_client(*args, **kwargs)

        monkeypatch.setattr(server.httpx, "AsyncClient", factory)

    return install


async def test_list_indexes_compacts_rows(api) -> None:
    api({"/indexes": httpx.Response(200, json=[
        {"id": "war", "version": 3, "title": "War", "yaml_sha256": "x", "owner": None},
    ])})
    out = await server.list_indexes()
    assert out["summary"] == "1 live indexes"
    assert out["data"] == [{"id": "war", "version": 3, "title": "War"}]


async def test_api_error_detail_is_passed_through(api) -> None:
    api({"/indexes/missing/score": httpx.Response(
        404, json={"detail": {"error": {"code": "INDEX_NOT_FOUND", "hint": "list_indexes"}}}
    )})
    out = await server.get_index_score("missing")
    assert out == {"error": {"code": "INDEX_NOT_FOUND", "hint": "list_indexes"}}


async def test_unreachable_api_returns_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(boom)}),
    )
    out = await server.list_indexes()
    assert out["error"]["code"] == "PMI_API_UNREACHABLE"


async def test_history_downsamples_and_keeps_last_point(api) -> None:
    points = [
        {"as_of": f"2026-01-01T00:{i:02d}:00Z", "score": float(i), "component_count": 5}
        for i in range(60)
    ]
    api({"/indexes/war/score/history": httpx.Response(
        200, json={"summary": "", "data": {"index_id": "war", "version": 1, "points": points}}
    )})
    out = await server.get_index_history("war", days=7)
    data = out["data"]
    assert data["delta"] == 59.0
    assert len(data["points"]) <= 26
    assert data["points"][-1]["score"] == 59.0


async def test_explain_ranks_by_relevancy_then_volume(api) -> None:
    comps = [
        {"title": "low", "relevancy": 0.1, "volume_24h": 9e9},
        {"title": "high", "relevancy": 0.9, "volume_24h": 1.0},
        {"title": "tie-busy", "relevancy": 0.5, "volume_24h": 100.0},
        {"title": "tie-quiet", "relevancy": 0.5, "volume_24h": 1.0},
    ]
    api({"/indexes/war/explain": httpx.Response(200, json={
        "index_id": "war", "version": 1, "as_of": "2026-01-01T00:00:00Z",
        "score": 42.0, "components": comps,
    })})
    out = await server.explain_index("war", top=3)
    titles = [c["title"] for c in out["data"]["top_components"]]
    assert titles == ["high", "tie-busy", "tie-quiet"]
