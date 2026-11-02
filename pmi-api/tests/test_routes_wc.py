"""/wc/matchups route tests (no DB).

The World Cup ended on 2026-07-19 and the route was retired to a fixed empty
envelope. These tests pin that it stays empty and never opens a DB session:
`get_session` is overridden to fail, so any regression back to a live query
breaks the HTTP test.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pmi_api.routes import wc


@pytest.mark.asyncio
async def test_wc_matchups_returns_empty_envelope() -> None:
    env = await wc.wc_matchups(sweet_odds=2.5, min_edge=0.1, max_age_h=24)

    assert env.data.matchups == []
    assert env.data.teams == {}
    assert env.data.params.sweet_odds == 2.5
    assert env.data.params.min_edge == 0.1
    assert wc.TOURNAMENT_ENDED in env.summary


@pytest.mark.asyncio
async def test_wc_matchups_http_never_touches_db() -> None:
    from pmi_api.deps import get_session
    from pmi_api.main import app

    async def no_db():
        raise AssertionError("/wc/matchups must not open a DB session")
        yield  # pragma: no cover  (makes this an async generator dependency)

    app.dependency_overrides[get_session] = no_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/wc/matchups")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["matchups"] == []
    assert body["data"]["teams"] == {}
    assert body["data"]["params"] == {"sweet_odds": 3.0, "min_edge": 0.03}
