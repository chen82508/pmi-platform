"""War index (country → region → world) read API.

All endpoints are pure cache readers over ts_page_cache (JSONB, pre-rendered by
the ``war-snapshot`` worker every 10 min from the ``war-index-2026``
``geo_weighted_mean`` ``ts_index_scores``). No per-request computation —
sub-second map / country-detail / region / trends. Mirrors ``routes/maga.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pmi_core.models import CoreMarket, TsPageCache, TsPriceSnapshot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session
from pmi_api.page_cache import read_latest, trend_series
from pmi_api.schemas import (
    WarByCountryEnvelope,
    WarByCountryPayload,
    WarByRegionEnvelope,
    WarByRegionPayload,
    WarCountryDetailEnvelope,
    WarCountryDetailPayload,
    WarCountryRow,
    WarCountryTrendsEnvelope,
    WarCountryTrendsPayload,
    WarLastUpdatedEnvelope,
    WarLastUpdatedPayload,
    WarQuestionsEnvelope,
    WarQuestionsPayload,
    WarQuestionTrendsEnvelope,
    WarQuestionTrendsPayload,
    WarRegionDetailEnvelope,
    WarRegionDetailPayload,
    WarRegionRow,
    WarRegionTrendsEnvelope,
    WarRegionTrendsPayload,
    WarSummaryEnvelope,
    WarSummaryPayload,
    WarTrendPoint,
    WarWorldTrendsEnvelope,
    WarWorldTrendsPayload,
)

router = APIRouter(prefix="/war", tags=["war"])


async def _with_live_citations(session: AsyncSession, cached: dict) -> dict:
    """The daily note's cited contracts, re-priced at read time.

    The note is rebuilt once a day (00:10Z) from that morning's prices, but the
    page shows it beside 10-minute-fresh feeds — so a citation printed 16% while
    the same contract read 20% two sections up (2026-10-12). The prose keeps its
    day's numbers (it says so); the citations are current."""
    cites = list(cached.get("citations") or [])
    ids = [c.get("market_id") for c in cites if c.get("market_id")]
    if not ids:
        return cached
    rows = dict(
        (
            await session.execute(
                select(CoreMarket.id, CoreMarket.last_price).where(
                    CoreMarket.id.in_(ids), CoreMarket.last_price.isnot(None)
                )
            )
        ).all()
    )
    fresh = []
    for c in cites:
        p = rows.get(c.get("market_id"))
        fresh.append({**c, "yes_pct": round(float(p) * 100.0, 1)} if p is not None else c)
    return {**cached, "citations": fresh}

DEFAULT_CYCLE = 2026
_NOT_READY = "War cache not ready — run `pmi-workers run war-snapshot` first."


async def _trend_points(
    session: AsyncSession,
    cache_key: str,
    path: tuple[str, ...],
    as_of: datetime | None,
    days: int,
) -> list[WarTrendPoint]:
    """One point per hour (the hour's last snapshot), read from the war board's
    slim trend companion — see ``pmi_api.page_cache.trend_series``. ``version``
    marks the methodology version behind each point, so a def bump (v10→v15 moved
    world heat several points in one week) is distinguishable from a market move."""
    rows = await trend_series(session, cache_key, path, days, as_of=as_of)
    return [WarTrendPoint(date=d, value=v, version=ver) for d, v, ver in rows]


# ---------------------------------------------------------------------------
# Map + detail
# ---------------------------------------------------------------------------


@router.get("/by-country", response_model=WarByCountryEnvelope)
async def war_by_country(
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarByCountryEnvelope:
    """Per-country war heat (the choropleth) from pre-computed cache.
    ``as_of`` selects the newest snapshot at or before that instant."""
    payload = await read_latest(session, f"war:{cycle}", as_of)
    if not payload:
        raise HTTPException(status_code=503, detail=_NOT_READY)
    countries = {
        slug: WarCountryRow(**row) for slug, row in payload["countries"].items()
    }
    return WarByCountryEnvelope(
        summary=(
            f"War heat across {payload['n_countries']} countries; "
            f"world {payload['world_heat']}."
        ),
        data=WarByCountryPayload(
            as_of=as_of or datetime.now(UTC),
            countries=countries,
            world_heat=payload["world_heat"],
            n_countries=payload["n_countries"],
            n_regions=payload["n_regions"],
            n_markets=payload["n_markets"],
            n_attributions=payload.get("n_attributions"),
            exchanges=payload.get("exchanges", []),
            world_volume_24h=payload.get("world_volume_24h"),
            venue_volume=payload.get("venue_volume", {}),
            concentration=payload.get("concentration"),
            world_delta_24h=payload.get("world_delta_24h"),
            world_delta_7d=payload.get("world_delta_7d"),
            breadth_7d=payload.get("breadth_7d"),
            prices_as_of=payload.get("prices_as_of"),
        ),
    )


@router.get("/by-country/{slug}", response_model=WarCountryDetailEnvelope)
async def war_country_detail(
    slug: str = Path(..., min_length=2, max_length=64),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarCountryDetailEnvelope:
    """Single-country detail + the contributing contracts (cache-only)."""
    cached = await read_latest(session, f"war-country:{slug.lower()}:{cycle}")
    if not cached:
        raise HTTPException(status_code=503, detail=_NOT_READY)
    return WarCountryDetailEnvelope(
        summary=f"{cached['name']}: war heat {cached['heat']}.",
        data=WarCountryDetailPayload(
            slug=cached["slug"],
            name=cached["name"],
            region=cached.get("region"),
            heat=cached["heat"],
            n_markets=cached["n_markets"],
            contracts=cached.get("contracts", []),
            n_scoring=cached.get("n_scoring"),
            weighting=cached.get("weighting"),
            prices_as_of=cached.get("prices_as_of"),
        ),
    )


@router.get("/questions", response_model=WarQuestionsEnvelope)
async def war_questions(
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarQuestionsEnvelope:
    """Flat 'By Question' feed — every contributing war contract, deduped and
    sorted by relevancy (cache-only, rendered by war-snapshot)."""
    cached = await read_latest(session, f"war-questions:{cycle}")
    if not cached:
        raise HTTPException(status_code=503, detail=_NOT_READY)
    questions = cached.get("questions", [])
    # Older cache rows predate total/truncated — fall back to the list length.
    total = cached.get("total", len(questions))
    truncated = cached.get("truncated", False)
    return WarQuestionsEnvelope(
        summary=(
            f"Top {len(questions)} of {total} war contracts across all countries."
            if truncated
            else f"{total} war contracts across all countries."
        ),
        data=WarQuestionsPayload(
            cycle_year=cached.get("cycle_year", cycle),
            questions=questions,
            total=total,
            truncated=truncated,
            prices_as_of=cached.get("prices_as_of"),
        ),
    )


@router.get("/by-region", response_model=WarByRegionEnvelope)
async def war_by_region(
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarByRegionEnvelope:
    """Per-region war heat (the 6-region rollup) from cache.
    ``as_of`` selects the newest snapshot at or before that instant."""
    payload = await read_latest(session, f"war:{cycle}", as_of)
    if not payload:
        raise HTTPException(status_code=503, detail=_NOT_READY)
    regions = {rid: WarRegionRow(**row) for rid, row in payload["regions"].items()}
    return WarByRegionEnvelope(
        summary=f"War heat across {len(regions)} regions; world {payload['world_heat']}.",
        data=WarByRegionPayload(
            as_of=as_of or datetime.now(UTC),
            regions=regions,
            world_heat=payload["world_heat"],
            n_regions=payload["n_regions"],
            n_countries=payload["n_countries"],
            world_volume_24h=payload.get("world_volume_24h"),
            world_delta_24h=payload.get("world_delta_24h"),
            world_delta_7d=payload.get("world_delta_7d"),
        ),
    )


@router.get("/regions/{region_id}", response_model=WarRegionDetailEnvelope)
async def war_region_detail(
    region_id: str = Path(..., min_length=2, max_length=32),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarRegionDetailEnvelope:
    """One region's heat + its scored countries (from the national cache)."""
    payload = await read_latest(session, f"war:{cycle}")
    if not payload:
        raise HTTPException(status_code=503, detail=_NOT_READY)
    rid = region_id.lower()
    region = payload["regions"].get(rid)
    if region is None:
        raise HTTPException(status_code=404, detail=f"Region '{rid}' not in war cache.")
    countries = [
        WarCountryRow(**c)
        for c in payload["countries"].values()
        if c.get("region") == rid
    ]
    countries.sort(key=lambda c: -c.heat)
    return WarRegionDetailEnvelope(
        summary=f"{region['name']}: war heat {region['heat']} ({len(countries)} scored).",
        data=WarRegionDetailPayload(
            region=rid,
            name=region["name"],
            heat=region["heat"],
            n_entities=region["n_entities"],
            n_scored=region["n_scored"],
            n_markets=region.get("n_markets", 0),
            n_attributions=region.get("n_attributions"),
            n_holdings=region.get("n_holdings", 0),
            exchanges=region.get("exchanges", []),
            countries=countries,
        ),
    )


# ---------------------------------------------------------------------------
# Trends (ts_page_cache history)
# ---------------------------------------------------------------------------


@router.get("/by-country/{slug}/trends", response_model=WarCountryTrendsEnvelope)
async def war_country_trends(
    slug: str = Path(..., min_length=2, max_length=64),
    days: int = Query(default=14, ge=1, le=90),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarCountryTrendsEnvelope:
    """Per-country heat trend from the national cache history."""
    key = slug.lower()
    points = await _trend_points(
        session, f"war:{cycle}", ("countries", key, "heat"), as_of, days
    )
    return WarCountryTrendsEnvelope(
        summary=f"{key}: {len(points)} daily heat points over {days}d.",
        data=WarCountryTrendsPayload(slug=key, days=days, points=points),
    )


@router.get("/regions/{region_id}/trends", response_model=WarRegionTrendsEnvelope)
async def war_region_trends(
    region_id: str = Path(..., min_length=2, max_length=64),
    days: int = Query(default=14, ge=1, le=90),
    metric: str = Query(default="heat"),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarRegionTrendsEnvelope:
    """Per-region trend from the national cache history. ``metric=volume``
    plots the region's 24h book instead of heat — the chart's volume lane
    (D5): the series accrues forward from 2026-09-13, when the snapshot began
    writing region volume, so early windows are honestly short."""
    key = region_id.lower()
    field = "volume_24h" if metric == "volume" else "heat"
    points = await _trend_points(
        session, f"war:{cycle}", ("regions", key, field), as_of, days
    )
    return WarRegionTrendsEnvelope(
        summary=f"{key}: {len(points)} hourly {metric} points over {days}d.",
        data=WarRegionTrendsPayload(region=key, days=days, points=points),
    )


@router.get("/trends", response_model=WarWorldTrendsEnvelope)
async def war_world_trends(
    days: int = Query(default=14, ge=1, le=90),
    metric: str = Query(default="heat"),
    as_of: datetime | None = Query(default=None),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarWorldTrendsEnvelope:
    """World trend from the national cache history. ``metric=volume`` plots
    the world 24h book (D5, forward-only since 2026-09-13)."""
    path: tuple[str, ...] = ("world_volume_24h",) if metric == "volume" else ("world_heat",)
    points = await _trend_points(session, f"war:{cycle}", path, as_of, days)
    return WarWorldTrendsEnvelope(
        summary=f"World war {metric}: {len(points)} hourly points over {days}d.",
        data=WarWorldTrendsPayload(days=days, points=points),
    )


@router.get("/questions/{market_id}/trends", response_model=WarQuestionTrendsEnvelope)
async def war_question_trends(
    market_id: int = Path(..., ge=1),
    days: int = Query(default=30, ge=1, le=90),
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> WarQuestionTrendsEnvelope:
    """A question anchor's facing-market price over time — its 'Market %' line.

    Reads the facing market's own price history from ``ts_price_snapshots`` (one
    point per day, the day's last snapshot), so the question page shows a trend
    chart consistent with its Market % tile. Unlike the country/region/world
    trends (which read the war cache's per-day heat), this is a raw market price
    because a question's headline number IS its facing market's probability.
    """
    cutoff = as_of or datetime.now(UTC)
    window_start = cutoff - timedelta(days=days)
    hour = func.date_trunc("hour", TsPriceSnapshot.snapshot_at)
    rows = (
        await session.execute(
            select(hour.label("t"), TsPriceSnapshot.last_price)
            .where(
                TsPriceSnapshot.market_id == market_id,
                TsPriceSnapshot.snapshot_at >= window_start,
                TsPriceSnapshot.snapshot_at <= cutoff,
                TsPriceSnapshot.last_price.isnot(None),
            )
            .distinct(hour)
            .order_by(hour, TsPriceSnapshot.snapshot_at.desc())
        )
    ).all()
    points = [
        WarTrendPoint(date=t.isoformat(), value=round(float(p) * 100.0, 1))
        for t, p in rows
    ]
    return WarQuestionTrendsEnvelope(
        summary=f"market {market_id}: {len(points)} hourly price points over {days}d.",
        data=WarQuestionTrendsPayload(market_id=market_id, days=days, points=points),
    )


@router.get("/summary", response_model=WarSummaryEnvelope)
async def war_summary(
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarSummaryEnvelope:
    """The world page's daily note (D6) — deterministic paragraphs + cited
    contracts, rebuilt 00:10Z by the war-summary job. 503 until the job's
    first run; the frontend soft-hides the section."""
    cached = await read_latest(session, f"war-summary:{cycle}")
    if not cached:
        raise HTTPException(
            status_code=503,
            detail="War summary not ready — run `pmi-workers run war-summary` first.",
        )
    cached = await _with_live_citations(session, cached)
    return WarSummaryEnvelope(
        summary=f"Daily war note, {len(cached.get('paragraphs', []))} paragraphs.",
        data=WarSummaryPayload(**cached),
    )


@router.get("/regions/{region_id}/summary", response_model=WarSummaryEnvelope)
async def war_region_summary(
    region_id: str = Path(..., min_length=2, max_length=32),
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarSummaryEnvelope:
    """One region's daily note (D6) — same shape as /war/summary."""
    cached = await read_latest(session, f"war-region-summary:{region_id.lower()}:{cycle}")
    if not cached:
        raise HTTPException(
            status_code=503,
            detail="War region summary not ready — run `pmi-workers run war-summary` first.",
        )
    cached = await _with_live_citations(session, cached)
    return WarSummaryEnvelope(
        summary=f"{region_id}: daily note, {len(cached.get('paragraphs', []))} paragraphs.",
        data=WarSummaryPayload(**cached),
    )


@router.get("/last-updated", response_model=WarLastUpdatedEnvelope)
async def war_last_updated(
    cycle: int = Query(default=DEFAULT_CYCLE),
    session: AsyncSession = Depends(get_session),
) -> WarLastUpdatedEnvelope:
    """When the war cache was last refreshed."""
    generated_at = await session.scalar(
        select(func.max(TsPageCache.computed_at)).where(
            TsPageCache.cache_key == f"war:{cycle}"
        )
    )
    return WarLastUpdatedEnvelope(
        summary=(
            f"War data last updated {generated_at.isoformat()}."
            if generated_at
            else "No war cache yet — run `pmi-workers run war-snapshot`."
        ),
        data=WarLastUpdatedPayload(generated_at=generated_at),
    )
