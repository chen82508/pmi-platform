"""Integration tests: auth gate mounted on real /indexes and /sources/health routes.

Companion to ``test_deps_auth.py`` (which only exercises the dependency on a
throw-away one-route app). This file wires the dep onto the real ``app`` and
verifies:

* ``GET /health`` stays public regardless of the flag (cloud-platform
  healthchecks don't know API keys).
* Every ``/indexes/*`` route + ``/sources/health`` rejects missing / invalid
  keys with 401 and accepts a known-good key with 200, *when*
  ``PMI_API_REQUIRE_AUTH=true``.
* With auth disabled (the dev default), the same routes are reachable
  without any key.

These tests intentionally do NOT use the shared ``client`` fixture from
``conftest.py``, because that one overrides ``require_api_key`` to a no-op.
Here we want the real dependency wired so we can prove it actually blocks.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pmi_core.models import CoreApiKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pmi_api.config import api_settings


@pytest_asyncio.fixture
async def real_app_client(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[AsyncClient]:
    """Real ``app`` with only the DB session overridden — auth dep is live."""
    from pmi_api.deps import get_session
    from pmi_api.main import app

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="session")
async def live_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Insert a real API key into the test DB once per session, return the raw token.

    Session-scoped because the test DB is session-scoped — function-scoping
    would re-insert the same key_hash across tests and trip
    ``uq_core_api_keys__key_hash``. Distinct prefix so it doesn't collide
    with ``seeded_data``'s placeholder rows (which use opaque ``"d"*64``
    hashes that no raw key can match).
    """
    raw = "pmi_live_routes_test_token_xyz"
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    async with session_factory() as s:
        s.add(
            CoreApiKey(
                key_prefix=raw[:8],
                key_hash=key_hash,
                label="routes-auth-test",
                is_active=True,
            )
        )
        await s.commit()
    return raw


# ──────────────────────────────────────────────────────────────────────────
# /health — must stay public regardless of the flag
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_public_when_auth_required(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liveness probe must work for cloud platforms that can't send API keys."""
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ──────────────────────────────────────────────────────────────────────────
# /sources/health — auth-gated
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sources_health_blocked_without_key(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get("/sources/health")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sources_health_allowed_with_valid_key(
    real_app_client: AsyncClient,
    live_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get(
        "/sources/health", headers={"X-API-Key": live_key}
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ──────────────────────────────────────────────────────────────────────────
# /indexes — auth-gated at router level
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_indexes_list_blocked_without_key(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get("/indexes")
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_indexes_score_blocked_with_invalid_key(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get(
        "/indexes/polymarket-war-index/score",
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_indexes_list_allowed_with_valid_key(
    real_app_client: AsyncClient,
    live_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get("/indexes", headers={"X-API-Key": live_key})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert any(i["id"] == "polymarket-war-index" for i in body)


@pytest.mark.asyncio
async def test_indexes_score_allowed_with_valid_key(
    real_app_client: AsyncClient,
    live_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get(
        "/indexes/polymarket-war-index/score",
        headers={"X-API-Key": live_key},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["index_id"] == "polymarket-war-index"


# ──────────────────────────────────────────────────────────────────────────
# Auth-disabled mode (dev default) — everything passes through
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_indexes_open_when_auth_disabled(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PMI_API_REQUIRE_AUTH=false is the dev convenience: dep is a no-op."""
    monkeypatch.setattr(api_settings, "require_auth", False)
    resp = await real_app_client.get("/indexes")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sources_health_open_when_auth_disabled(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", False)
    resp = await real_app_client.get("/sources/health")
    assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# /admin/prompts — auth-gated at router level (was leaking the prompt IP:
# GET had no key dependency, only PUT did, so the factor templates + bound
# model ids were readable unauthenticated wherever :8000 was reachable).
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_prompts_blocked_without_key(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: GET /admin/prompts used to return 200 + templates here."""
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get("/admin/prompts")
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_prompts_allowed_with_valid_key(
    real_app_client: AsyncClient,
    live_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_settings, "require_auth", True)
    resp = await real_app_client.get(
        "/admin/prompts", headers={"X-API-Key": live_key}
    )
    assert resp.status_code == 200
    assert "directly_about_war" in resp.json()


@pytest.mark.asyncio
async def test_admin_prompts_open_when_auth_disabled(
    real_app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev (auth off) keeps the editor reachable — the SG is the control there."""
    monkeypatch.setattr(api_settings, "require_auth", False)
    resp = await real_app_client.get("/admin/prompts")
    assert resp.status_code == 200
