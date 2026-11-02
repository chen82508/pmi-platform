"""MAGA-by-state read API.

All endpoints are pure cache readers over ts_page_cache (JSONB, pre-rendered by
the `maga-snapshot` worker every 10 min from the YAML `ts_index_scores` engine).
No per-request computation — sub-second map / state-detail / groups / trends.
The legacy on-demand `state_lean` / `aggregate_state_detail` recompute path was
retired 2026-06-24 (it would have served stale non-YAML numbers on cache miss).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pmi_core.engine.incumbency import incumbent_party
from pmi_core.models import TsPageCache
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest, trend_series
from pmi_api.schemas import (
    MagaByStateEnvelope,
    MagaByStatePayload,
    MagaGroupRow,
    MagaGroupsEnvelope,
    MagaGroupsPayload,
    MagaLastUpdatedEnvelope,
    MagaLastUpdatedPayload,
    MagaNationalTrendsEnvelope,
    MagaNationalTrendsPayload,
    MagaRaceContractRow,
    MagaStateDetailEnvelope,
    MagaStateDetailPayload,
    MagaTrendPoint,
    MagaTrendsEnvelope,
    MagaTrendsPayload,
    StateLeanRow,
)

router = APIRouter(prefix="/maga", tags=["maga"])

DEFAULT_CYCLE = 2026


def _seat_key(g: dict) -> str:
    """Incumbency lookup key for a race group: house is per-district (``CA-20``,
    at-large → district 1), senate/governor by 2-letter state. Matches how the
    roster is keyed so house groups resolve their HELD BY / flip chip."""
    if g.get("office") == "house":
        return f"{g['state_code']}-{g.get('district') or 1}"
    return g["state_code"]


# ---------------------------------------------------------------------------
# Endpoints (cache reads via pmi_api.page_cache — shared with routes/war.py)
# ---------------------------------------------------------------------------

@router.get("/by-state", response_model=MagaByStateEnvelope)
async def maga_by_state(
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaByStateEnvelope:
    """Per-state partisan lean from pre-computed cache. < 1ms.
    ``as_of`` selects the newest snapshot at or before that instant."""
    payload = await read_latest(session, f"maga:{cycle}", as_of)
    if not payload:
        raise HTTPException(status_code=503, detail="MAGA cache not ready — run maga-snapshot job first.")

    states = {
        code: StateLeanRow(
            state=s["state"], state_code=code, heat=s["heat"],
            n_markets=s["n_markets"], offices=s["offices"], volume_24h=s["volume_24h"],
            exchanges=s.get("exchanges", []),
            office_heat=s.get("office_heat", {}),
            delta_14d=s.get("delta_14d"),
        )
        for code, s in payload["states"].items()
    }
    return MagaByStateEnvelope(
        summary=f"MAGA lean across {payload['n_states']} states; national {payload['national_heat']}.",
        data=MagaByStatePayload(
            as_of=as_of or datetime.now(UTC),
            states=states,
            national_heat=payload["national_heat"],
            n_states=payload["n_states"],
            n_markets=payload["n_markets"],
            exchanges=payload.get("exchanges", []),
            chambers=payload.get("chambers", {}),
            volume_24h=payload.get("volume_24h"),
            delta_24h=payload.get("delta_24h"),
            delta_14d=payload.get("delta_14d"),
        ),
    )


@router.get("/by-state/{state_code}", response_model=MagaStateDetailEnvelope)
async def maga_state_detail(
    state_code: str = Path(..., min_length=2, max_length=2),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaStateDetailEnvelope:
    """State detail — reads the per-state cache (written by maga-snapshot from the
    YAML ts_index_scores every 10 min). Cache-only: no on-demand recompute."""
    code = state_code.upper()
    cached = await read_latest(session, f"maga-state:{code}:{cycle}")
    if not cached:
        raise HTTPException(status_code=503, detail="MAGA cache not ready.")

    group_rows = [
        MagaGroupRow(
            state=g["state"], state_code=g["state_code"],
            office=g["office"], district=g.get("district"),
            heat=g["heat"], n_markets=g["n_markets"], volume_24h=g["volume_24h"],
            base_question=g["base_question"],
            contracts=[MagaRaceContractRow(**c) for c in g.get("contracts", [])],
            exchanges=g.get("exchanges", []),
            incumbent_party=incumbent_party(g["office"], _seat_key(g), cycle),
            delta_14d=g.get("delta_14d"),
            venue_lineage=g.get("venue_lineage", []),
            no_market=g.get("no_market"),
            prior_applied=g.get("prior_applied"),
        )
        for g in cached.get("groups", [])
    ]
    return MagaStateDetailEnvelope(
        summary=f"{cached['state']}: MAGA lean {cached['heat']}.",
        data=MagaStateDetailPayload(
            state=cached["state"], state_code=code,
            heat=cached["heat"], n_markets=cached["n_markets"],
            volume_24h=cached["volume_24h"], offices=cached["offices"],
            groups=group_rows,
        ),
    )


@router.get("/groups", response_model=MagaGroupsEnvelope)
async def maga_groups(
    chamber: str | None = Query(default=None),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaGroupsEnvelope:
    """All race groups from cache.
    ``as_of`` selects the newest snapshot at or before that instant."""
    payload = await read_latest(session, f"maga:{cycle}", as_of)
    if not payload:
        raise HTTPException(status_code=503, detail="MAGA cache not ready.")

    want = chamber.lower() if chamber else None
    groups = [
        MagaGroupRow(
            state=g["state"], state_code=g["state_code"], office=g["office"],
            district=g.get("district"), heat=g["heat"], n_markets=g["n_markets"],
            volume_24h=g["volume_24h"], base_question=g["base_question"], contracts=[],
            exchanges=g.get("exchanges", []),
            incumbent_party=incumbent_party(g["office"], _seat_key(g), cycle),
            delta_14d=g.get("delta_14d"),
            venue_lineage=g.get("venue_lineage", []),
            no_market=g.get("no_market"),
            prior_applied=g.get("prior_applied"),
        )
        for g in payload["groups"]
        if not want or want in ("all", "state") or g["office"] == want
    ]
    groups.sort(key=lambda r: -r.heat)

    n_states = len({g.state_code for g in groups})
    n_markets = sum(g.n_markets for g in groups)
    return MagaGroupsEnvelope(
        summary=f"{len(groups)} race groups across {n_states} states.",
        data=MagaGroupsPayload(
            as_of=as_of or datetime.now(UTC),
            groups=groups, n_states=n_states, n_markets=n_markets,
        ),
    )


@router.get("/by-state/{state_code}/trends", response_model=MagaTrendsEnvelope)
async def maga_state_trends(
    state_code: str = Path(..., min_length=2, max_length=2),
    days: int = Query(default=14, ge=1, le=90),
    chamber: str | None = Query(default=None),
    district: str | None = Query(default=None),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaTrendsEnvelope:
    """Per-state trend from the MAGA board's slim trend companion (one point per
    hour) — see ``pmi_api.page_cache.trend_series``."""
    code = state_code.upper()
    # When a chamber is given, plot THAT office's heat so the trend matches the
    # office-detail headline tile; otherwise the state-level aggregate. Falls
    # back gracefully — a snapshot with no value for the deeper path contributes
    # no point there (null rows are skipped), so the series fills forward.
    if chamber == "house" and district:
        # A single congressional district — plot only that district's heat.
        path: tuple[str, ...] = ("states", code, "district_heat", district.zfill(2))
    elif chamber in ("senate", "governor", "house"):
        path = ("states", code, "office_heat", chamber)
    else:
        path = ("states", code, "heat")
    rows = await trend_series(session, f"maga:{cycle}", path, days, as_of=as_of)
    points = [MagaTrendPoint(date=d, value=v) for d, v, _ver in rows]
    return MagaTrendsEnvelope(
        summary=f"{code}: {len(points)} hourly heat points over {days}d.",
        data=MagaTrendsPayload(state_code=code, days=days, points=points),
    )


@router.get("/trends", response_model=MagaNationalTrendsEnvelope)
async def maga_national_trends(
    days: int = Query(default=14, ge=1, le=90),
    chamber: str | None = Query(default=None),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaNationalTrendsEnvelope:
    """National trend from the MAGA board's slim trend companion (hourly points).

    ``chamber`` (senate|house|governor) plots that component's own national PMI
    (``chambers.{ch}.pmi``) instead of the combined national_heat — the v4
    per-office history pane / 90d sparklines (D7). ``trend_series`` reads the slim
    ``maga-trend:{cycle}`` key and falls back to the full ``maga:{cycle}`` rows
    only for the part of the window the slim series has not reached yet."""
    path: tuple[str, ...] = (
        ("chambers", chamber, "pmi")
        if chamber in ("senate", "house", "governor")
        else ("national_heat",)
    )
    rows = await trend_series(session, f"maga:{cycle}", path, days, as_of=as_of)
    points = [MagaTrendPoint(date=d, value=v) for d, v, _ver in rows]
    return MagaNationalTrendsEnvelope(
        summary=f"National MAGA heat: {len(points)} hourly points over {days}d.",
        data=MagaNationalTrendsPayload(days=days, points=points),
    )


# Governor board retired from /maga (2026-06-24): it now uses the same generic
# /indexes/us-governor-2026-republican-seats/senate-board path as Senate & House
# (the governor-board-snapshot worker writes the senate-board:<id> cache).


@router.get("/last-updated", response_model=MagaLastUpdatedEnvelope)
async def maga_last_updated(
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> MagaLastUpdatedEnvelope:
    """When MAGA cache was last refreshed."""
    generated_at = await session.scalar(
        select(func.max(TsPageCache.computed_at))
        .where(TsPageCache.cache_key == f"maga:{cycle}")
    )
    return MagaLastUpdatedEnvelope(
        summary=(
            f"MAGA data last updated {generated_at.isoformat()}." if generated_at
            else "No MAGA cache yet — run `pmi-workers run maga-snapshot`."
        ),
        data=MagaLastUpdatedPayload(generated_at=generated_at),
    )
