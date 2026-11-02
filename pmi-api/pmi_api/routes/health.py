"""/health — liveness + DB connectivity probe."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pmi_core.models import AuditSourceHealth
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key
from pmi_api.schemas import SourceHealthRow

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    """Public liveness probe — no auth, used by cloud platform healthchecks."""
    db_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@router.get(
    "/sources/health",
    response_model=list[SourceHealthRow],
    dependencies=[Depends(require_api_key)],
)
async def sources_health(session: AsyncSession = Depends(get_session)) -> list[AuditSourceHealth]:
    """Per-source poller health. Surfaces internal state, so auth-gated."""
    rows = (await session.execute(select(AuditSourceHealth))).scalars().all()
    return list(rows)
