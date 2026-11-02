"""Insights vertical — GET /insights (current issue) + /insights/{iso_week}.

Cache-backed like every vertical: the insights-snapshot worker freezes each
week's issue under `insight:issue:{iso_week}` and mirrors the current one to
`insight:latest`. These endpoints serve the frozen snapshot in one read; there is
no request-path compute (the gap-pairs self-join is far too heavy for the hot
path). "Past issues" is the list of frozen week keys.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest

router = APIRouter(prefix="/insights", tags=["insights"])

# Distinct frozen issues, newest first: the latest row per insight:issue:* key,
# with just the fields the "Past issues" list renders (no full payload).
# The WHERE clause must stay identical to the predicate of the partial index
# ix_ts_page_cache__insight_issue (alembic 0034); any other wording falls back
# to a full seq scan of ts_page_cache.
_PAST_SQL = text(
    """
    SELECT DISTINCT ON (cache_key) cache_key,
           payload->>'iso_week'   AS iso_week,
           payload->>'issue_date' AS issue_date,
           payload->'lead'->>'spread_now'  AS spread,
           payload->'lead'->'high'->>'chamber' AS high_chamber,
           payload->'lead'->'low'->>'chamber'  AS low_chamber
    FROM ts_page_cache
    WHERE cache_key LIKE 'insight:issue:%'
    ORDER BY cache_key, computed_at DESC
    """
)


async def _past_issues(session: AsyncSession, exclude: str | None) -> list[dict]:
    rows = (await session.execute(_PAST_SQL)).all()
    issues = []
    for r in rows:
        if r.iso_week == exclude:
            continue
        headline = None
        if r.spread and r.high_chamber and r.low_chamber:
            headline = f"{r.high_chamber}–{r.low_chamber} chamber gap {float(r.spread):.1f} pts"
        issues.append({"iso_week": r.iso_week, "issue_date": r.issue_date, "headline": headline})
    issues.sort(key=lambda i: i["issue_date"] or "", reverse=True)
    return issues


@router.get("")
async def insights(session: AsyncSession = Depends(get_session)) -> dict:
    """The current Insights issue + the list of past frozen issues."""
    issue = await read_latest(session, "insight:latest")
    if issue is None:
        return {
            "summary": "Insights: no issue computed yet",
            "data": {"issue_date": None, "lead": None, "gap_pairs": {"pairs": []},
                     "settling": {"items": []}, "readings": [], "past_issues": []},
        }
    lead = issue.get("lead") or {}
    past = await _past_issues(session, exclude=issue.get("iso_week"))
    n_gaps = len((issue.get("gap_pairs") or {}).get("pairs", []))
    return {
        "summary": (
            f"Micah Insights {issue.get('issue_date')}: "
            + (f"chamber gap {lead.get('spread_now')} pts, " if lead else "")
            + f"{n_gaps} cross-venue gaps, {len(issue.get('readings') or [])} readings"
        ),
        "data": {**issue, "past_issues": past},
    }


@router.get("/{iso_week}")
async def insight_issue(iso_week: str, session: AsyncSession = Depends(get_session)) -> dict:
    """One frozen past issue by ISO week (e.g. "2026-W37")."""
    issue = await read_latest(session, f"insight:issue:{iso_week}")
    if issue is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "ISSUE_NOT_FOUND", "hint": "GET /insights lists past_issues"}},
        )
    past = await _past_issues(session, exclude=iso_week)
    return {"summary": f"Micah Insights {issue.get('issue_date')} (archived)", "data": {**issue, "past_issues": past}}
