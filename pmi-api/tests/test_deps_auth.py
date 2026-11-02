"""Tests for ``pmi_api.deps.require_api_key`` — CORR-0.8 regression.

The pre-fix bug: ``session: AsyncSession = None`` was never injected by
FastAPI, so every authenticated request silently opened a second
``SessionLocal()`` outside the dependency graph. After the fix the session
comes from ``Depends(get_session)`` and shares the transaction context with
the route handler — which is essential once we start wiring auth onto
routes that also write (e.g. last-used bumps).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pmi_core.models import CoreApiKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pmi_api.config import api_settings
from pmi_api.deps import get_session, require_api_key


@pytest_asyncio.fixture
async def auth_client(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncClient:
    """Build a throw-away FastAPI app exposing one route guarded by require_api_key."""
    monkeypatch.setattr(api_settings, "require_auth", True)

    app = FastAPI()

    @app.get("/secure")
    async def secure(
        principal: CoreApiKey | None = Depends(require_api_key),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        # Also touch the session to prove a single shared session works.
        from sqlalchemy import text

        ping = (await session.execute(text("SELECT 1"))).scalar_one()
        return {"label": principal.label if principal else None, "ping": ping}

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def real_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Insert a real API key whose sha256 we know, return the raw key."""
    raw = "live-test-key-1234567890"
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    async with session_factory() as s:
        s.add(
            CoreApiKey(
                key_prefix=raw[:8],
                key_hash=key_hash,
                label="live",
                is_active=True,
            )
        )
        await s.commit()
    return raw


@pytest.mark.asyncio
async def test_missing_key_rejected(auth_client: AsyncClient) -> None:
    async with auth_client as ac:
        resp = await ac.get("/secure")
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_key_rejected(auth_client: AsyncClient) -> None:
    async with auth_client as ac:
        resp = await ac.get("/secure", headers={"X-API-Key": "definitely-not-a-key"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_valid_key_accepted_and_shares_session(
    auth_client: AsyncClient, real_key: str
) -> None:
    """CORR-0.8: the auth dependency receives the same session as the route
    (via the dependency graph), so the route's session.execute() works and
    the principal's `label` is propagated back."""
    async with auth_client as ac:
        resp = await ac.get("/secure", headers={"X-API-Key": real_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"label": "live", "ping": 1}


@pytest.mark.asyncio
async def test_auth_disabled_returns_none_principal(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PMI_API_REQUIRE_AUTH=false` short-circuits to a no-op."""
    monkeypatch.setattr(api_settings, "require_auth", False)
    async with auth_client as ac:
        resp = await ac.get("/secure")
    assert resp.status_code == 200
    assert resp.json()["label"] is None
