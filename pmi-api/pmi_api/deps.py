"""FastAPI dependencies: session, API key check."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, status
from pmi_core.db import SessionLocal
from pmi_core.models import CoreApiKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.config import api_settings


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session scoped to the request.

    Route handlers should perform their own ``await session.commit()`` when
    they mutate rows; ``get_session`` itself does not commit on yield exit,
    so downstream code can rely on a single commit point per request.
    """
    async with SessionLocal() as session:
        yield session


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> CoreApiKey | None:
    """At P0 dev mode (``PMI_API_REQUIRE_AUTH=false``), this is a no-op.

    When auth is enabled, the session is injected through FastAPI's
    dependency graph (CORR-0.8 fix: was previously ``session=None`` which
    silently opened a second, undeclared ``SessionLocal()`` per request and
    bypassed transaction sharing with route handlers).
    """
    if not api_settings.require_auth:
        return None
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key")
    return await _check_key(session, x_api_key)


async def _check_key(session: AsyncSession, raw_key: str) -> CoreApiKey:
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    row = (
        await session.execute(
            select(CoreApiKey).where(
                CoreApiKey.key_hash == key_hash,
                CoreApiKey.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    row.last_used_at = datetime.now(UTC)
    # The shared session yielded by ``get_session`` does not auto-commit;
    # commit here so the ``last_used_at`` update is durable. Routes that
    # only read can stay commit-less.
    await session.commit()
    return row
