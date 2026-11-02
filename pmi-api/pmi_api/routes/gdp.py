"""US GDP Expectation vertical — GET /gdp/board + /gdp/period/{period}.

Cache-backed like every other vertical: the gdp-board-snapshot worker
pre-computes the board (`gdp-board:{id}`) and one blob per period
(`gdp-period:{id}:{period}` = anchor + contracts + hourly series) into
ts_page_cache; these endpoints serve it in a single read. On a cold cache they
fall back to computing once via `pmi_core.engine.gdp_compute` (the same code the
worker runs) — the request path no longer recomputes build_board or re-scores
the per-hour trend (the period page was ~6s).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pmi_core.engine.gdp_compute import GDP_INDEX_ID, compute_gdp
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest, scalar_points, trend_series

router = APIRouter(prefix="/gdp", tags=["gdp"])

_CACHE_KEY = f"gdp-board:{GDP_INDEX_ID}"


def _series_span_days(series: list[dict]) -> float:
    """Days a [{date, value}] series (ISO dates) spans; 0 if not comparable."""
    dates = [p["date"] for p in series if p.get("date")]
    if len(dates) < 2:
        return 0.0
    try:
        return (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).total_seconds() / 86400
    except ValueError:
        return 0.0


@router.get("/board")
async def gdp_board(session: AsyncSession = Depends(get_session)) -> dict:
    """US GDP Expectation board — headline sentiment, annual term-structure, and
    every (period) anchor for drill-down. Read from cache; the sentiment trend is
    this key's own hourly history."""
    board = await read_latest(session, _CACHE_KEY)
    if board is None:  # cold cache — compute once
        board, _ = await compute_gdp(session)

    rows = await trend_series(session, _CACHE_KEY, ("score",), 90)
    board = {**board, "sentiment_series": [{"date": d, "value": v} for d, v, _ in rows]}

    e = board.get("e_growth")
    return {
        "summary": (
            f"US GDP Expectation = {board['score']} ({board['regime']}); "
            f"E[growth] {e}% across {board['n_anchors']} periods"
            if board.get("score") is not None
            else "US GDP Expectation: no priced GDP markets found"
        ),
        "data": board,
    }


@router.get("/period/{period}")
async def gdp_period(period: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Drill-down for one anchor period (e.g. "2027" or "2026Q2"): the anchor's
    folded reading + every underlying GDP contract + the hourly series. Served
    from the pre-computed period blob."""
    detail = await read_latest(session, f"gdp-period:{GDP_INDEX_ID}:{period}")
    if detail is None:  # cold cache — compute once
        _, periods = await compute_gdp(session)
        detail = periods.get(period)
    if detail is None:
        return {
            "summary": f"US GDP growth {period}: no priced contracts",
            "data": {"period": period, "anchor": None, "contracts": [], "series": []},
        }

    # 90D via scalar-cache (calendar-gated): the blob's `series` is the backfilled
    # 14d re-score (heavy at 90d), while gdp-period-anchor accrues this period's
    # reading cheaply. Serve whichever spans MORE, so the 90D chip lights up as the
    # cache fills, with no near-term regression (the 14d series wins until it does).
    scalar = await scalar_points(
        session, f"gdp-period-anchor:{GDP_INDEX_ID}:{period}", ("reading",),
        datetime.now(UTC), 90, bucket="hour",
    )
    long_series = [{"date": iso, "value": round(float(v), 1)} for iso, v, _ver in scalar]
    if _series_span_days(long_series) > _series_span_days(detail.get("series") or []):
        detail = {**detail, "series": long_series}

    anchor = detail.get("anchor")
    contracts = detail.get("contracts", [])
    return {
        "summary": (
            f"US GDP growth {period}: E[growth] {anchor['e_growth']}% → "
            f"sentiment {anchor['sentiment']} across {len(contracts)} contracts"
            if anchor
            else f"US GDP growth {period}: no priced contracts"
        ),
        "data": detail,
    }
