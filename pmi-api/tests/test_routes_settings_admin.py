"""Route tests for /settings and /admin/prompts (legacy SPA parity endpoints).

The admin editor surfaces `core_prompts` (latest version per name) and saves
edits as NEW append-only versions — never in-place updates. The conftest
fixture seeds exactly one prompt: ("directly_about_war", v1, "dummy prompt
body"), with no active factor model bound, so `model`/`temperature` are None.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_settings_returns_future_phrase(client: AsyncClient) -> None:
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert resp.json() == {"future_phrase": "within the next 12 months"}


@pytest.mark.asyncio
async def test_admin_prompts_lists_latest_versions(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/admin/prompts")
    assert resp.status_code == 200
    body = resp.json()

    assert "directly_about_war" in body
    record = body["directly_about_war"]
    # Startswith: a PUT test in this module may already have appended a
    # "(edited)" version to the shared seeded DB.
    assert record["content"].startswith("dummy prompt body")
    # No active factor model bound to the seeded prompt → display fields None.
    assert record["model"] is None
    assert record["temperature"] is None
    assert record["top_p"] is None
    assert record["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_admin_prompts_save_appends_new_version(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    # The seeded DB is shared across tests in this module, so anchor on the
    # CURRENT latest content rather than assuming version numbers.
    before = (await client.get("/admin/prompts")).json()
    new_content = before["directly_about_war"]["content"] + " (edited)"

    payload = {"directly_about_war": {"content": new_content}}
    resp = await client.put("/admin/prompts", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["new_versions"]["directly_about_war"] >= 2

    # The editor now reads back the new latest version.
    after = (await client.get("/admin/prompts")).json()
    assert after["directly_about_war"]["content"] == new_content


@pytest.mark.asyncio
async def test_admin_prompts_save_unchanged_is_noop(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    current = (await client.get("/admin/prompts")).json()
    payload = {"directly_about_war": {"content": current["directly_about_war"]["content"]}}
    resp = await client.put("/admin/prompts", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "new_versions": {}}


@pytest.mark.asyncio
async def test_admin_prompts_save_creates_unknown_name_at_v1(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    payload = {"brand-new-prompt": {"content": "fresh body"}}
    resp = await client.put("/admin/prompts", json=payload)
    assert resp.status_code == 200
    assert resp.json()["new_versions"] == {"brand-new-prompt": 1}

    after = (await client.get("/admin/prompts")).json()
    assert after["brand-new-prompt"]["content"] == "fresh body"
