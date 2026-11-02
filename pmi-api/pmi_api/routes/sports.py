"""Sports vertical read API — served entirely from the `sports-snapshot` cache.

Cache-only by design: the board is pre-computed into `ts_page_cache` every
~10 min, and these endpoints only `read_latest` it. A cold cache returns an
"updating" state rather than a slow live fold, so a request never does per-request
compute (the hot-path rule the whole platform follows).

`GET /sports/{sport}/board` — one sport's whole vertical. Keyed per sport so
/sports/mlb is its own page and more sports slot in beside it (P1: MLB only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest

router = APIRouter(prefix="/sports", tags=["sports"])


@router.get("/{sport}/board")
async def board(sport: str, session: AsyncSession = Depends(get_session)) -> dict:
    """One sport's board (P1: MLB — World Series leaderboard + bracket)."""
    payload = await read_latest(session, f"sports-board:{sport}")
    if not payload:
        return {
            "summary": f"{sport} board updating",
            "data": {"sport": sport, "outrights": [], "bracket": None, "games": []},
        }
    outs = payload.get("outrights", [])
    teams = sum(len(o.get("teams", [])) for o in outs)
    bracket = payload.get("bracket")
    games = payload.get("games", [])
    parts = [f"{len(outs)} boards · {teams} teams"]
    if games:
        parts.append(f"{len(games)} games")
    if bracket:
        parts.append(bracket["round_label"])
    return {"summary": " · ".join(parts), "data": payload}
