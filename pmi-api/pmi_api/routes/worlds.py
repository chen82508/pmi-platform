"""LoL Worlds vertical read API — served entirely from the `worlds-snapshot` cache.

Cache-only by design, like every other vertical: the board + per-match detail are
pre-computed into `ts_page_cache` every ~10 min (`worlds-board:{tid}`,
`worlds-match:{slug}`) and these endpoints only `read_latest` them. A cold cache
returns an "updating" state rather than a slow live fold.

  GET /worlds/board                — the whole tournament (structure + odds + results)
  GET /worlds/match/{key}          — one match: head-to-head + backing contracts
  GET /worlds/match/{key}/trend    — that match's series win % over time

The match detail reuses the esports response models (a Worlds match IS an esports
LoL match fold), so the esports match-page shell renders it unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest
from pmi_api.schemas import (
    EsportsHolding,
    EsportsMatch,
    EsportsMatchDetailEnvelope,
    EsportsMatchDetailPayload,
    EsportsTrendEnvelope,
    EsportsTrendPayload,
    EsportsTrendPoint,
)

router = APIRouter(prefix="/worlds", tags=["worlds"])

_DEFAULT_TID = "lol-worlds-2026"


@router.get("/board")
async def board(
    tid: str = _DEFAULT_TID, session: AsyncSession = Depends(get_session)
) -> dict:
    """The LoL Worlds board (structure + live matches + resolved results)."""
    payload = await read_latest(session, f"worlds-board:{tid}")
    if not payload:
        return {
            "summary": "worlds board updating",
            "data": {"id": tid, "stages": [], "final": None},
        }
    stages = payload.get("stages", [])
    live = sum(len(s.get("live_matches", [])) for s in stages)
    results = sum(len(s.get("results", [])) for s in stages)
    parts = [f"{len(stages)} stages"]
    if live:
        parts.append(f"{live} live")
    if results:
        parts.append(f"{results} decided")
    return {"summary": " · ".join(parts), "data": payload}


async def _detail(session: AsyncSession, key: str, tid: str) -> dict | None:
    """Cached match detail; `key` is the slug, falling back to a board scan for a
    numeric key / market_id."""
    detail = await read_latest(session, f"worlds-match:{key}")
    if detail is not None:
        return detail
    board_payload = await read_latest(session, f"worlds-board:{tid}")
    if not board_payload:
        return None
    for st in board_payload.get("stages", []):
        for m in st.get("live_matches", []):
            if key in (m.get("key"), m.get("slug")) or key in {str(i) for i in m.get("market_ids", [])}:
                return await read_latest(session, f"worlds-match:{m['slug']}")
        for r in st.get("results", []):
            if key in (r.get("key"), r.get("slug")):
                return await read_latest(session, f"worlds-match:{r['slug']}")
    return None


@router.get("/match/{key}", response_model=EsportsMatchDetailEnvelope)
async def match_detail(
    key: str, tid: str = _DEFAULT_TID, session: AsyncSession = Depends(get_session)
) -> EsportsMatchDetailEnvelope:
    """One Worlds match: the cross-venue head-to-head + every backing contract."""
    detail = await _detail(session, key, tid)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No Worlds match '{key}'.")
    match = detail["match"]
    holdings = [EsportsHolding(**h) for h in detail.get("holdings", [])]
    return EsportsMatchDetailEnvelope(
        summary=(
            f"{match['team_a']} vs {match['team_b']} — "
            f"{len(holdings)} contracts across {len(match['venues'])} exchange(s)."
        ),
        data=EsportsMatchDetailPayload(
            generated_at=detail.get("generated_at") or datetime.now(UTC),
            match=EsportsMatch(**match),
            holdings=holdings,
        ),
    )


@router.get("/match/{key}/trend", response_model=EsportsTrendEnvelope)
async def match_trend(
    key: str,
    tid: str = _DEFAULT_TID,
    days: int = Query(default=14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
) -> EsportsTrendEnvelope:
    """The match favourite's series win % over time (served from the snapshot)."""
    detail = await _detail(session, key, tid)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No Worlds match '{key}'.")
    trend = detail.get("trend") or {}
    points = [EsportsTrendPoint(**p) for p in trend.get("points", [])]
    return EsportsTrendEnvelope(
        summary=f"{trend.get('favourite')}: {len(points)} hourly win% points.",
        data=EsportsTrendPayload(
            generated_at=detail.get("generated_at") or datetime.now(UTC),
            favourite=trend.get("favourite"),
            days=trend.get("days", days),
            points=points,
        ),
    )
