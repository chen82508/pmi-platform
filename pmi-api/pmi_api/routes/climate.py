"""Climate & Weather vertical — GET /climate/hub + /climate/board/{board}.

Cache-backed like every other vertical: the climate-snapshot worker pre-computes
the hub (`climate-hub`) and one blob per sub-board (`climate-board:{board}` =
score + anchors + holdings) into ts_page_cache; these endpoints serve it in a
single read. On a cold cache they fall back to computing once via
`pmi_core.engine.climate_compute` (the same code the worker runs) — never a
recompute on the request path. The hub score's own history feeds its trend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pmi_core.engine.climate_compute import CLIMATE_BOARDS, compute_climate
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest, trend_series

router = APIRouter(prefix="/climate", tags=["climate"])


@router.get("/hub")
async def climate_hub(session: AsyncSession = Depends(get_session)) -> dict:
    """Climate & Weather hub — headline (Global Warming) score + a card per
    sub-board. Served from cache; the headline trend is this key's own history."""
    hub = await read_latest(session, "climate-hub")
    if hub is None:  # cold cache — compute once
        hub, _ = await compute_climate(session)

    rows = await trend_series(session, "climate-hub", ("score",), 90)
    hub = {**hub, "score_series": [{"date": d, "value": v} for d, v, _ in rows]}

    n = len(hub.get("boards", []))
    return {
        "summary": (
            f"Climate & Weather = {hub['score']} (Global Warming) across {n} boards"
            if hub.get("score") is not None
            else "Climate & Weather: no priced markets found"
        ),
        "data": hub,
    }


@router.get("/board/{board}")
async def climate_board(board: str, session: AsyncSession = Depends(get_session)) -> dict:
    """One sub-board (e.g. "global-warming"): headline score + anchor questions +
    every backing contract (play-money included, weighted 0). The board score's
    trend is this key's own hourly history."""
    detail = await read_latest(session, f"climate-board:{board}")
    # Only fall back to a (costly) full recompute for a KNOWN board on a cold
    # cache; an unknown/removed board name (e.g. an old "hurricanes" deep-link)
    # returns not-found immediately instead of recomputing the whole vertical.
    if detail is None and board in CLIMATE_BOARDS:
        _, boards = await compute_climate(session)
        detail = boards.get(board)
    if detail is None:
        return {
            "summary": f"Climate board {board}: not found",
            "data": {"board": board, "score": None, "anchors": [], "holdings": []},
        }

    rows = await trend_series(session, f"climate-board:{board}", ("score",), 90)
    detail = {**detail, "score_series": [{"date": d, "value": v} for d, v, _ in rows]}

    return {
        "summary": (
            f"{detail.get('title', board)} = {detail['score']} "
            f"({detail['component_count']} contracts)"
            if detail.get("score") is not None
            else f"{detail.get('title', board)}: no priced contracts"
        ),
        "data": detail,
    }


@router.get("/question/{key}")
async def climate_question(key: str, session: AsyncSession = Depends(get_session)) -> dict:
    """One anchor drill-in (key = "{board}__{anchor}"): its folded reading, every
    backing contract (play-money included), and the reading's hourly trend (this
    key's own cache history)."""
    detail = await read_latest(session, f"climate-question:{key}")
    if detail is None:  # cold cache — compute once (known boards only)
        board_id, _, anchor = key.partition("__")
        if board_id in CLIMATE_BOARDS:
            _, boards = await compute_climate(session)
            q = (boards.get(board_id) or {}).get("questions", {}).get(anchor)
            detail = {**q, "board": board_id, "key": anchor} if q else None
    if detail is None:
        return {
            "summary": f"Climate question {key}: not found",
            "data": {"key": key, "label": key, "reading": None, "unit": "PMI", "contracts": [], "series": []},
        }

    rows = await trend_series(session, f"climate-question:{key}", ("reading",), 90)
    detail = {**detail, "series": [{"date": d, "value": v} for d, v, _ in rows]}

    return {
        "summary": f"{detail.get('label', key)} = {detail.get('reading')} ({detail.get('n_contracts', 0)} contracts)",
        "data": detail,
    }
