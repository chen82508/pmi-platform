"""Smoke tests for /health and /sources/health."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "db": True}


@pytest.mark.asyncio
async def test_sources_health_empty(client: AsyncClient) -> None:
    """No source rows seeded → endpoint returns an empty list, not a 500."""
    resp = await client.get("/sources/health")
    assert resp.status_code == 200
    assert resp.json() == []
