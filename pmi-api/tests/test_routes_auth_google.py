"""Route tests: POST /auth/google — the Google SSO account upsert.

Uses the shared ``client`` fixture (require_api_key overridden to a no-op, as
for every other route suite); the gate itself is proven generically in
test_routes_auth.py / test_deps_auth.py, and /auth mounts the same dependency
at router level.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_google_login_creates_then_refreshes(client: AsyncClient) -> None:
    """First sign-in inserts; a later sign-in updates the same row in place."""
    first = await client.post(
        "/auth/google",
        json={
            "google_sub": "sub-created-1",
            "email": "Jane@Example.com",
            "name": "Jane",
            "picture_url": "https://lh3.googleusercontent.com/a/p1",
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert "new account" in body["summary"]
    data = body["data"]
    assert isinstance(data["id"], int)
    assert data["google_sub"] == "sub-created-1"
    # Address is normalised to lowercase before insert.
    assert data["email"] == "jane@example.com"
    assert data["name"] == "Jane"
    assert data["created_at"] is not None
    assert data["last_login_at"] is not None

    second = await client.post(
        "/auth/google",
        json={
            "google_sub": "sub-created-1",
            "email": "jane.new@example.com",
            "name": "Jane D.",
            "picture_url": "https://lh3.googleusercontent.com/a/p2",
        },
    )
    assert second.status_code == 200
    body2 = second.json()
    assert "new account" not in body2["summary"]
    data2 = body2["data"]
    # Same account (stable subject), refreshed profile.
    assert data2["id"] == data["id"]
    assert data2["created_at"] == data["created_at"]
    assert data2["email"] == "jane.new@example.com"
    assert data2["name"] == "Jane D."
    assert data2["picture_url"] == "https://lh3.googleusercontent.com/a/p2"
    assert data2["last_login_at"] >= data["last_login_at"]


@pytest.mark.asyncio
async def test_google_login_profile_fields_optional(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/google",
        json={"google_sub": "sub-minimal-1", "email": "min@example.com"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] is None
    assert data["picture_url"] is None


@pytest.mark.asyncio
async def test_google_login_rejects_bad_email(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/google",
        json={"google_sub": "sub-bad-email", "email": "not-an-email"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_EMAIL"


@pytest.mark.asyncio
async def test_google_login_rejects_blank_sub(client: AsyncClient) -> None:
    """Whitespace-only sub passes pydantic's min_length; the route must catch it."""
    resp = await client.post(
        "/auth/google",
        json={"google_sub": "   ", "email": "ws@example.com"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_SUB"


@pytest.mark.asyncio
async def test_google_login_missing_sub_is_422(client: AsyncClient) -> None:
    resp = await client.post("/auth/google", json={"email": "nosub@example.com"})
    assert resp.status_code == 422
