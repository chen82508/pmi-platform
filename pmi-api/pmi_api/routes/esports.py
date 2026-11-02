"""Esports match browser — GET /esports/matches + /esports/matches/{key}.

Cache-backed like every other vertical: the `esports-snapshot` worker pre-folds
the board + per-match detail (holdings + trend) into ts_page_cache every ~10 min
(`esports-board`, `esports-match:{slug}`), and these endpoints serve it in a
single latest-wins read. On a cold cache (snapshot not yet run) they fall back to
computing live once — same code, via `pmi_core.engine.esports_compute`.

The combined win % reads off Kalshi + Robinhood + Polymarket last prices; no LLM,
no score row. Games: CS2 / Valorant / LoL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pmi_core.engine.esports_compute import GAME_TOKENS, compute_esports
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest, trend_series
from pmi_api.schemas import (
    EsportsHolding,
    EsportsMatch,
    EsportsMatchDetailEnvelope,
    EsportsMatchDetailPayload,
    EsportsMatchesEnvelope,
    EsportsMatchesPayload,
    EsportsTrendEnvelope,
    EsportsTrendPayload,
    EsportsTrendPoint,
    EsportsVolumeEnvelope,
    EsportsVolumePayload,
    EsportsVolumePoint,
)

router = APIRouter(prefix="/esports", tags=["esports"])

_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _board(session: AsyncSession) -> dict:
    """Cached match board; compute live once on a cold cache."""
    board = await read_latest(session, "esports-board")
    if board is None:
        board, _ = await compute_esports(session)
    return board


async def _detail(session: AsyncSession, key: str) -> dict | None:
    """Cached match detail (match + holdings + trend). `key` is normally the
    readable slug; fall back to scanning the board for a numeric key / market_id,
    and to a live compute on a fully cold cache."""
    detail = await read_latest(session, f"esports-match:{key}")
    if detail is not None:
        return detail
    board = await _board(session)
    match = next(
        (
            m
            for m in board.get("matches", [])
            if key in (m["key"], m["slug"]) or key in {str(i) for i in m["market_ids"]}
        ),
        None,
    )
    if match is None:
        return None
    detail = await read_latest(session, f"esports-match:{match['slug']}")
    if detail is not None:
        return detail
    _, details = await compute_esports(session)  # cold cache — compute once
    return details.get(match["slug"])


async def _resolve_slug(session: AsyncSession, key: str) -> str | None:
    """The esports-match cache slug for ``key`` (a slug passes straight through;
    a numeric key / market_id is resolved via the board)."""
    if await read_latest(session, f"esports-match:{key}") is not None:
        return key
    board = await _board(session)
    m = next(
        (
            m
            for m in board.get("matches", [])
            if key in (m["key"], m["slug"]) or key in {str(i) for i in m["market_ids"]}
        ),
        None,
    )
    return m["slug"] if m else None


@router.get("/matches", response_model=EsportsMatchesEnvelope)
async def esports_matches(
    game: str | None = Query(default=None, description="cs2 | valorant | lol (omit = all)"),
    max_age_h: int = Query(default=48, ge=1, le=168),
    started_grace_h: int = Query(
        default=12, ge=0, le=168,
        description="Hide matches that started more than this many hours ago (0 = keep all).",
    ),
    session: AsyncSession = Depends(get_session),
) -> EsportsMatchesEnvelope:
    """Upcoming / live esports matches with a cross-venue market-implied win %."""
    games = [game.lower()] if game else list(GAME_TOKENS)
    valid = {g for g in games if g in GAME_TOKENS}
    if game and not valid:
        return EsportsMatchesEnvelope(
            summary=f"No such game '{game}'. Try: {', '.join(GAME_TOKENS)}.",
            data=EsportsMatchesPayload(
                generated_at=datetime.now(UTC), game=game, matches=[], n=0
            ),
        )

    board = await _board(session)
    matches = [m for m in board.get("matches", []) if m["game"] in valid]
    settled = [m for m in board.get("settled", []) if m["game"] in valid]
    # Hide matches whose real start is well in the past (a venue keeps a match's
    # market open ~2 days after it's played). Same filter as before, in-memory.
    if started_grace_h:
        grace = datetime.now(UTC) - timedelta(hours=started_grace_h)
        matches = [
            m for m in matches
            if not m["starts_at"] or _parse_iso(m["starts_at"]) >= grace  # type: ignore[operator]
        ]
    matches.sort(key=lambda m: (m["starts_at"] is None, m["starts_at"] or "", -m["volume_24h"]))

    parsed = [EsportsMatch(**m) for m in matches]
    n_cross = sum(1 for m in parsed if len(m.venues) > 1)
    label = game or " + ".join(GAME_TOKENS)
    return EsportsMatchesEnvelope(
        summary=(
            f"{len(parsed)} live {label} matches, {n_cross} priced on multiple "
            f"exchanges (cross-venue win %)."
        ),
        data=EsportsMatchesPayload(
            generated_at=board.get("generated_at") or datetime.now(UTC),
            game=game,
            matches=parsed,
            n=len(parsed),
            settled=settled,
        ),
    )


@router.get("/matches/{key}", response_model=EsportsMatchDetailEnvelope)
async def esports_match_detail(
    key: str,
    session: AsyncSession = Depends(get_session),
) -> EsportsMatchDetailEnvelope:
    """One match: the cross-venue head-to-head + every backing contract (winner
    legs, per-map, total-maps, handicap) across the exchanges pricing it."""
    detail = await _detail(session, key)
    if detail is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"No live esports match '{key}'.")
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


@router.get("/matches/{key}/trend", response_model=EsportsTrendEnvelope)
async def esports_match_trend(
    key: str,
    days: int = Query(default=14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
) -> EsportsTrendEnvelope:
    """The favourite's win % over time — served from the pre-computed match
    detail (14d hourly). `days` is accepted for compatibility but the snapshot
    fixes the window; the trend is no longer computed on the request path."""
    detail = await _detail(session, key)
    if detail is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"No live esports match '{key}'.")
    trend = detail.get("trend") or {}
    points = [EsportsTrendPoint(**p) for p in trend.get("points", [])]
    return EsportsTrendEnvelope(
        summary=f"{trend.get('favourite')}: {len(points)} hourly win% points.",
        data=EsportsTrendPayload(
            generated_at=detail.get("generated_at") or datetime.now(UTC),
            favourite=trend.get("favourite") or match_favourite(detail),
            days=trend.get("days", days),
            points=points,
        ),
    )


@router.get("/matches/{key}/volume", response_model=EsportsVolumeEnvelope)
async def esports_match_volume(
    key: str,
    days: int = Query(default=1, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
) -> EsportsVolumeEnvelope:
    """The match's 24h book sampled hourly from the esports-match cache history —
    the spotlight sparkbar. Cadence-safe (reads the cache key's own history, no
    ts_price_snapshots scan); the hourly series accrues forward from deploy."""
    slug = await _resolve_slug(session, key)
    now = datetime.now(UTC)
    rows = (
        await trend_series(session, f"esports-match:{slug}", ("match", "volume_24h"), days)
        if slug
        else []
    )
    points = [EsportsVolumePoint(date=d, value=v) for d, v, _ver in rows]
    # % change first→last over the window (skip leading/trailing nulls).
    first = next((p.value for p in points if p.value), None)
    last = next((p.value for p in reversed(points) if p.value is not None), None)
    change_pct = (
        round((last - first) / first * 100.0, 1)
        if first and last is not None
        else None
    )
    return EsportsVolumeEnvelope(
        summary=f"{slug or key}: {len(points)} hourly book points over {days}d.",
        data=EsportsVolumePayload(
            generated_at=now, days=days, points=points, change_pct=change_pct
        ),
    )


def match_favourite(detail: dict) -> str:
    return detail.get("match", {}).get("favorite") or ""
