"""/indexes — list, get, score (current + history + on-demand), explain, backtest."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pmi_core import queue
from pmi_core.dsl.ir import IndexDef
from pmi_core.engine.aggregator import _direction_value, _relevancy
from pmi_core.engine.anchor_trend import compute_anchor_trends
from pmi_core.engine.incumbency import incumbent_party
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.engine.markets_board_compute import (
    _freshest_snapshot_before,
    compute_markets_board,
)
from pmi_core.engine.peer_grouping import group_peers
from pmi_core.engine.seat_distribution import (
    band_counts,
    classify_band,
    compute_seat_distribution,
)
from pmi_core.engine.seat_mapping import (
    VENUE_PRIORITY,
    extract_contested_seats,
    parse_extraction_label,
    parse_seat_race,
    resolve_candidate_party,
)
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreJob,
    CoreMarket,
    TsIndexScore,
    TsPageCache,
)
from pmi_core.workflow import create_run
from sqlalchemy import BigInteger, desc, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key
from pmi_api.page_cache import board_history_points, read_latest, scalar_points
from pmi_api.schemas import (
    AnchorTrendEnvelope,
    AnchorTrendPayload,
    BoardTrendPoint,
    ExplainComponent,
    ExplainPayload,
    HistoryEnvelope,
    HistoryPayload,
    HistoryPoint,
    IndexSummary,
    JobEnvelope,
    JobPayload,
    MarketsBoardEnvelope,
    MarketsBoardPayload,
    RaceTrendEnvelope,
    RaceTrendPayload,
    ScoreEnvelope,
    ScorePayload,
    SenateBoardEnvelope,
    SenateBoardPayload,
    SenateRace,
    WorkflowRunEnvelope,
    WorkflowRunPayload,
)

router = APIRouter(
    prefix="/indexes",
    tags=["indexes"],
    dependencies=[Depends(require_api_key)],
)


async def _current_def(session: AsyncSession, index_id: str) -> CoreIndexDefinition:
    row = (
        await session.execute(
            select(CoreIndexDefinition)
            .where(
                CoreIndexDefinition.index_id == index_id,
                CoreIndexDefinition.is_current.is_(True),
            )
            .order_by(CoreIndexDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        # Fall back to highest version (in case is_current wasn't set during the
        # very first pipeline run — defensive at P0).
        row = (
            await session.execute(
                select(CoreIndexDefinition)
                .where(CoreIndexDefinition.index_id == index_id)
                .order_by(CoreIndexDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "INDEX_NOT_FOUND", "hint": "Try GET /indexes."}},
        )
    return row


@router.get("", response_model=list[IndexSummary])
async def list_indexes(session: AsyncSession = Depends(get_session)) -> list[CoreIndexDefinition]:
    rows = (
        await session.execute(
            select(CoreIndexDefinition)
            .where(CoreIndexDefinition.is_current.is_(True))
            .order_by(CoreIndexDefinition.index_id)
        )
    ).scalars().all()
    if not rows:
        rows = (
            await session.execute(
                select(CoreIndexDefinition).order_by(
                    CoreIndexDefinition.index_id,
                    CoreIndexDefinition.version.desc(),
                )
            )
        ).scalars().all()
    return list(rows)


@router.get("/{index_id}", response_model=IndexSummary)
async def get_index(
    index_id: str, session: AsyncSession = Depends(get_session)
) -> CoreIndexDefinition:
    return await _current_def(session, index_id)


async def _component_provenance(
    session: AsyncSession, score: TsIndexScore
) -> tuple[int, list[str]]:
    """How many contracts this reading is made of, and on which venues.

    Both read off `component_evaluation_ids` — the score's own lineage — rather
    than the definition, so they describe the reading that exists rather than
    the one the YAML permits. That matters twice:

    * `component_count` is NOT a contract count. It counts whatever the formula
      aggregates over: seats for `seat_projection_sum`, countries for
      `geo_weighted_mean`, markets only for the weighted averages. The War
      Index's 87 is 87 countries priced by 558 contracts, so a UI that labels
      `component_count` "contracts" is wrong by a factor of six for that index
      and exactly right for the others.
    * an index with no `venues:` (the default — every real-money venue) can't
      name its exchanges from the definition at all.

    Distinct markets, because the lineage holds one row per (market, factor).
    """
    ids = list(score.component_evaluation_ids or [])
    if not ids:
        return 0, []
    # unnest(:array) rather than IN (...): the War Index's lineage is ~4.5k ids
    # (558 markets x 8 factors), and this is on the landing page's path.
    eval_ids = select(
        func.unnest(literal(ids, type_=ARRAY(BigInteger))).label("id")
    ).subquery()
    rows = (
        await session.execute(
            select(
                CoreMarket.venue,
                func.count(func.distinct(AuditEvaluation.market_id)).label("n"),
            )
            .select_from(eval_ids)
            .join(AuditEvaluation, AuditEvaluation.id == eval_ids.c.id)
            .join(CoreMarket, CoreMarket.id == AuditEvaluation.market_id)
            .group_by(CoreMarket.venue)
            .order_by(desc("n"))
        )
    ).all()
    return sum(r.n for r in rows), [r.venue for r in rows if r.venue]


def _score_envelope(
    index_def: CoreIndexDefinition,
    score: TsIndexScore,
    provenance: tuple[int, list[str]] = (0, []),
) -> ScoreEnvelope:
    market_count, venues = provenance
    score_val = float(score.score) if score.score is not None else None
    score_str = f"{score_val:.2f}" if score_val is not None else "n/a"
    return ScoreEnvelope(
        summary=(
            f"{index_def.title} (v{index_def.version}) = {score_str} "
            f"across {score.component_count} components as of "
            f"{score.as_of.isoformat()}"
        ),
        data=ScorePayload(
            index_id=index_def.index_id,
            version=index_def.version,
            as_of=score.as_of,
            score=score_val,
            component_count=score.component_count,
            market_count=market_count,
            venues=venues,
            computed_at=score.computed_at,
            breakdown=score.breakdown,
        ),
    )


async def _latest_score(
    session: AsyncSession, index_definition_id: int, as_of: datetime | None = None
) -> TsIndexScore | None:
    stmt = select(TsIndexScore).where(
        TsIndexScore.index_definition_id == index_definition_id
    )
    if as_of is not None:
        stmt = stmt.where(TsIndexScore.as_of <= as_of)
    return (
        await session.execute(stmt.order_by(desc(TsIndexScore.as_of)).limit(1))
    ).scalar_one_or_none()


async def _enqueue_score_job(session: AsyncSession, index_id: str) -> CoreJob:
    """Enqueue one on-demand pipeline tick. Dedupe means a storm of stale
    requests funds a single computation; commit so the worker sees it
    (NOTIFY is transactional and fires at this commit)."""
    job = await queue.enqueue(
        session,
        "score",
        {"index_id": index_id},
        dedupe_key=f"score:{index_id}",
        priority=queue.PRIORITY_INTERACTIVE,
    )
    await queue.notify(session)
    await session.commit()
    return job


def _job_202(job: CoreJob, hint: str) -> JSONResponse:
    envelope = JobEnvelope(
        summary=f"{hint} job {job.id} is {job.status}; poll GET /jobs/{job.id}",
        data=JobPayload.model_validate(job),
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=jsonable_encoder(envelope))


@router.get("/{index_id}/score", response_model=ScoreEnvelope)
async def get_score(
    index_id: str,
    as_of: datetime | None = Query(default=None),
    max_age_s: int | None = Query(
        default=None,
        ge=1,
        description=(
            "§3.2 on-demand path: if the latest stored score is older than "
            "this many seconds, enqueue a recompute. Returns 202 + job_id "
            "(or, with wait_s, blocks until the fresh score lands)."
        ),
    ),
    wait_s: float | None = Query(
        default=None,
        ge=0.5,
        le=120,
        description="With max_age_s: wait synchronously up to this long for the recompute.",
    ),
    session: AsyncSession = Depends(get_session),
) -> ScoreEnvelope:
    index_def = await _current_def(session, index_id)
    score = await _latest_score(session, index_def.id, as_of)

    # Cache semantics (§3.2, Postgres-backed): the latest ts_index_scores row
    # IS the cache. Fresh enough (or no freshness asked, or historical query)
    # → serve it.
    if max_age_s is None or as_of is not None:
        if score is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "NO_SCORE_YET",
                        "hint": "POST /indexes/{id}/score/refresh (or wait for next tick).",
                    }
                },
            )
        return _score_envelope(index_def, score, await _component_provenance(session, score))

    now = datetime.now(UTC)
    if score is not None and score.as_of >= now - timedelta(seconds=max_age_s):
        return _score_envelope(index_def, score, await _component_provenance(session, score))

    # Stale (or absent) → enqueue a tick.
    job = await _enqueue_score_job(session, index_id)

    if wait_s is None:
        return _job_202(job, f"score for '{index_id}' is stale; recompute")

    # Synchronous mode: poll until the job lands (or fails / times out).
    deadline = asyncio.get_event_loop().time() + wait_s
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        job = (
            await session.execute(
                select(CoreJob)
                .where(CoreJob.id == job.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if job.status == "succeeded":
            fresh = await _latest_score(session, index_def.id)
            if fresh is not None:
                return _score_envelope(
                    index_def, fresh, await _component_provenance(session, fresh)
                )
        if job.status == "failed":
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "RECOMPUTE_FAILED",
                        "hint": f"GET /jobs/{job.id} for the error detail",
                    }
                },
            )
    return _job_202(job, f"recompute for '{index_id}' still pending after {wait_s}s;")


@router.post("/{index_id}/score/refresh", response_model=JobEnvelope, status_code=202)
async def refresh_score(
    index_id: str,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Force-enqueue a pipeline tick for this index (deduped). 202 + job_id."""
    index_def = await _current_def(session, index_id)
    job = await _enqueue_score_job(session, index_def.index_id)
    return _job_202(job, f"recompute for '{index_def.index_id}'")


@router.post("/{index_id}/backtest", response_model=WorkflowRunEnvelope, status_code=202)
async def start_backtest(
    index_id: str,
    days: int = Query(default=90, ge=1, le=730),
    step_hours: int = Query(default=24, ge=1, le=168),
    version: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Start a durable backtest workflow (CORR-8.1). 202 + workflow_run_id;
    poll GET /workflows/{id}, download GET /workflows/{id}/csv when done.

    Pin `version` to replay a specific definition (default: current). Replaying
    several versions over the same window diffs them in the MLflow UI — each
    backtest is mirrored to one run under experiment ``pmi.<index_id>`` (SHIP-3.6).
    """
    if version is None:
        index_def = await _current_def(session, index_id)
    else:
        index_def = (
            await session.execute(
                select(CoreIndexDefinition).where(
                    CoreIndexDefinition.index_id == index_id,
                    CoreIndexDefinition.version == version,
                )
            )
        ).scalar_one_or_none()
        if index_def is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "INDEX_VERSION_NOT_FOUND",
                        "hint": f"index '{index_id}' has no version {version}. Try GET /indexes.",
                    }
                },
            )
    args: dict = {
        "index_id": index_def.index_id,
        "days": days,
        "step_hours": step_hours,
    }
    if version is not None:  # keep args minimal — absent key → workflow default
        args["version"] = version
    run = await create_run(session, "backtest", args)
    job = await queue.enqueue(
        session,
        "workflow",
        {"workflow_run_id": run.id},
        dedupe_key=f"workflow:{run.id}",
    )
    run.job_id = job.id
    await queue.notify(session)
    await session.commit()

    envelope = WorkflowRunEnvelope(
        summary=(
            f"backtest of '{index_def.index_id}' v{index_def.version} over {days}d "
            f"started; poll GET /workflows/{run.id}"
        ),
        data=WorkflowRunPayload.model_validate(run),
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content=jsonable_encoder(envelope)
    )


@router.get("/{index_id}/score/history", response_model=HistoryEnvelope)
async def get_history(
    index_id: str,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> HistoryEnvelope:
    index_def = await _current_def(session, index_id)
    # Only the three plotted columns: a full row drags the TOASTed `breakdown`
    # JSON and `component_evaluation_ids` array along (~87 MB for 8 days of war
    # ticks vs ~20 KB), which timed out on cold reads.
    stmt = select(
        TsIndexScore.as_of, TsIndexScore.score, TsIndexScore.component_count
    ).where(TsIndexScore.index_definition_id == index_def.id)
    if from_ is not None:
        stmt = stmt.where(TsIndexScore.as_of >= from_)
    if to is not None:
        stmt = stmt.where(TsIndexScore.as_of <= to)
    # The MOST RECENT `limit` rows in the window, returned ascending. `ORDER BY
    # as_of ASC LIMIT n` returns the OLDEST n instead — and any index scored more
    # than `limit` times in the window (btc/sp500 tick ~40x/day: hourly + on-demand
    # + drift re-eval, so 8 days ≫ 200) then has its newest points cut off, so its
    # sparkline/delta freeze at the n-th oldest row while /score stays fresh.
    stmt = stmt.order_by(TsIndexScore.as_of.desc()).limit(limit)
    rows = list(reversed((await session.execute(stmt)).all()))
    points = [
        HistoryPoint(
            as_of=r.as_of,
            score=float(r.score) if r.score is not None else None,
            component_count=r.component_count,
        )
        for r in rows
    ]
    return HistoryEnvelope(
        summary=f"{index_def.title} (v{index_def.version}): {len(points)} points",
        data=HistoryPayload(
            index_id=index_def.index_id, version=index_def.version, points=points
        ),
    )


@router.get("/{index_id}/explain", response_model=ExplainPayload)
async def explain_score(
    index_id: str,
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ExplainPayload:
    """Return the component breakdown of a particular score.

    P0 explanation: list the markets that fed into the latest score and their
    factor values. P1 wires an LLM synthesis pass (Visualisation M2).
    """
    index_def = await _current_def(session, index_id)
    stmt = select(TsIndexScore).where(TsIndexScore.index_definition_id == index_def.id)
    if as_of is not None:
        stmt = stmt.where(TsIndexScore.as_of <= as_of)
    stmt = stmt.order_by(desc(TsIndexScore.as_of)).limit(1)
    score = (await session.execute(stmt)).scalar_one_or_none()
    if score is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NO_SCORE_YET"}},
        )

    if not score.component_evaluation_ids:
        return ExplainPayload(
            index_id=index_def.index_id,
            version=index_def.version,
            as_of=score.as_of,
            score=float(score.score) if score.score is not None else None,
            components=[],
        )

    # Column projection, not full entities: the helpers below only read
    # value_numeric (+ value_label for the payload), while a full
    # AuditEvaluation row drags model_response JSON along — measured ~6.9MB
    # of ORM load per war-index request, none of it returned.
    eval_rows = (
        await session.execute(
            select(
                AuditEvaluation.market_id,
                AuditEvaluation.factor_id,
                AuditEvaluation.value_numeric,
                AuditEvaluation.value_label,
            ).where(AuditEvaluation.id.in_(score.component_evaluation_ids))
        )
    ).all()

    # Group rows by market so each market gets a dict[factor_id, eval-like]
    # that the shared `_relevancy` / `_direction_value` helpers can consume
    # directly (CORR-3.3 (c): single source of truth for the weighting math —
    # they only touch `.value_numeric`, which the namespace shim carries).
    by_market_evals: dict[int, dict[str, SimpleNamespace]] = {}
    by_market_factors: dict[int, dict[str, float | str | None]] = {}
    for market_id, factor_id, value_numeric, value_label in eval_rows:
        by_market_evals.setdefault(market_id, {})[factor_id] = SimpleNamespace(
            value_numeric=value_numeric
        )
        by_market_factors.setdefault(market_id, {})[factor_id] = (
            float(value_numeric) if value_numeric is not None else value_label
        )

    market_ids = list(by_market_evals.keys())
    market_rows = (
        await session.execute(
            select(CoreMarket.id, CoreMarket.title, CoreMarket.venue).where(
                CoreMarket.id.in_(market_ids)
            )
        )
    ).all() if market_ids else []
    title_by_id = {mid: title for mid, title, _venue in market_rows}
    venue_by_id = {mid: venue for mid, _title, venue in market_rows}

    # CORR-3.3 (b): surface the price that fed into the score — the freshest
    # snapshot at-or-before `score.as_of` per market, one LATERAL probe each
    # (the old DISTINCT ON walked every snapshot ≤ as_of per market). Also
    # pulls volume_24h from the same snapshot for the holdings cards.
    last_price_by_market: dict[int, float | None] = {}
    volume_by_market: dict[int, float | None] = {}
    if market_ids:
        for mid, (price, vol) in (
            await _freshest_snapshot_before(session, market_ids, score.as_of)
        ).items():
            last_price_by_market[mid] = price
            volume_by_market[mid] = vol

    # Parse the IR once so we can call the aggregator helpers with the typed shape.
    ir = IndexDef.model_validate(index_def.definition)

    # Cross-venue peer grouping: same question on several venues → one group,
    # so the holdings UI can fold the components into one multi-chip card.
    peer_group_by_market = await group_peers(session, market_ids)

    components = [
        ExplainComponent(
            market_id=mid,
            title=title_by_id.get(mid, "(unknown)"),
            last_price=last_price_by_market.get(mid),
            # CORR-3.3 (a): previously these were hard-coded to 0.0 because the
            # `setdefault` bucket was never mutated after construction. Now they
            # are computed from the loaded evaluations using the same helpers
            # the aggregator uses, so /explain matches /score exactly.
            relevancy=_relevancy(by_market_evals[mid], ir),
            direction=_direction_value(by_market_evals[mid], ir, title_by_id.get(mid)),
            factors=by_market_factors[mid],
            venue=venue_by_id.get(mid),
            volume_24h=volume_by_market.get(mid),
            peer_group_id=peer_group_by_market.get(mid, mid),
        )
        for mid in by_market_evals
    ]

    return ExplainPayload(
        index_id=index_def.index_id,
        version=index_def.version,
        as_of=score.as_of,
        score=float(score.score) if score.score is not None else None,
        components=components,
    )


@router.get("/{index_id}/markets-board", response_model=MarketsBoardEnvelope)
async def markets_board(
    index_id: str,
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> MarketsBoardEnvelope:
    """Board payload for the /micah/markets page: score + regime, driver
    buckets, composition weights, cross-version score series and the top
    holdings rows.

    Fast path: reads from ts_page_cache (written by markets-board-snapshot
    worker every 10 min against the main `pmi` DB — same cadence as every other
    vertical). Falls back to live computation when the cache is empty or an
    explicit as_of is given. The heavy compute now lives in
    `pmi_core.engine.markets_board_compute` so the worker and this route share
    one implementation; live-computing it on the hot path over the ~1M-row main
    `pmi` DB times out, which is exactly why the snapshot exists.
    """
    # ── Fast path: read from cache (no ts_price_snapshots scan) ─────────────
    if as_of is None:
        cached = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"markets-board:{index_id}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if cached is not None:
            payload = MarketsBoardPayload.model_validate(cached)
            return MarketsBoardEnvelope(
                summary=_markets_board_summary(payload, cached=True),
                data=payload,
            )

    # ── Slow path: live compute (as_of given or cache empty) ────────────────
    payload_dict = await compute_markets_board(session, index_id, as_of)
    if payload_dict is None:
        # compute_markets_board returns None for BOTH an unknown index and a
        # known index with no score yet. Disambiguate for the 404 code: the
        # route-local _current_def raises INDEX_NOT_FOUND when the id is unknown;
        # if it returns, the index exists but has no score.
        await _current_def(session, index_id)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NO_SCORE_YET", "hint": "Run the pipeline first."}},
        )
    payload = MarketsBoardPayload.model_validate(payload_dict)
    return MarketsBoardEnvelope(
        summary=_markets_board_summary(payload, cached=False),
        data=payload,
    )


def _markets_board_summary(payload: MarketsBoardPayload, *, cached: bool) -> str:
    score_txt = f"{payload.score:.1f}" if payload.score is not None else "n/a"
    tail = " (cached)" if cached else ""
    return (
        f"{payload.index_id} v{payload.version}: {score_txt} ({payload.regime}), "
        f"{payload.component_count} components across {payload.holdings} buckets{tail}."
    )


# Default chamber geometry when an index definition omits `aggregation.
# seat_projection` (CORR-1.2). Falls back to the US Senate shape with no
# holdover, which degrades to "every modelled seat is contested".
_SEAT_DEFAULTS = {
    "total_seats": 100,
    "majority_threshold": 51,
    "holdover_r": 0,
    "holdover_d": 0,
}

# Polymarket sets `groupItemTitle` to the candidate ("Jon Husted (R)") once a
# race has named nominees, and to a generic party/outcome label before that.
# Only the former is a matchup signal.
_GENERIC_GROUP_TITLES = {"republican", "democrat", "democratic", "yes", "no", "other"}


def _candidate_label(raw: dict | None) -> str | None:
    """Candidate name from a market's raw Gamma payload, or None if generic."""
    label = str((raw or {}).get("groupItemTitle") or "").strip()
    if not label or label.lower() in _GENERIC_GROUP_TITLES:
        return None
    return label


# compute_anchor_trends' window in markets-board-snapshot — the backfilled
# markets-trend spans this. The scalar cache only supersedes it once it accrues
# past this, so there's no near-term regression while the cache is filling.
_RECENT_TREND_DAYS = 30


def _iso_day(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return iso


async def _anchor_scalar_trend(
    session: AsyncSession, index_id: str, anchor: str
) -> tuple[AnchorTrendPayload, float] | None:
    """(payload, span_days) for the anchor's long trend from the scalar-cache
    (`markets-anchor:{index}:{anchor}`), or None if it hasn't accrued ≥2 points.

    markets-board-snapshot appends this anchor's current reading each run, so the
    key's own ts_page_cache history IS the trend — read cheaply via scalar_points
    (one indexed key, no ts_price_snapshots member scan). Calendar-gated: it spans
    only as far back as the cache has accrued (up to 90d). Labels are ISO (the
    frontend's parseDate handles them); span comes from those ISO stamps, not the
    markets-trend's human-formatted labels."""
    pts = await scalar_points(
        session, f"markets-anchor:{index_id}:{anchor}", ("reading",), datetime.now(UTC), 90, bucket="hour"
    )
    if len(pts) < 2:
        return None
    labels = [iso for iso, _v, _ver in pts]
    data: list[float | None] = [round(float(v), 1) for _iso, v, _ver in pts]
    span_days = (datetime.fromisoformat(labels[-1]) - datetime.fromisoformat(labels[0])).total_seconds() / 86400
    n = len(labels)
    payload = AnchorTrendPayload(
        data=data, labels=labels, xLabels=[_iso_day(labels[0]), _iso_day(labels[n // 2]), _iso_day(labels[-1])]
    )
    return payload, span_days


@router.get("/{index_id}/anchor-trend", response_model=AnchorTrendEnvelope)
async def anchor_trend(
    index_id: str,
    anchor: str = Query(..., description="anchor key, e.g. up:close:2026-08-19"),
    session: AsyncSession = Depends(get_session),
) -> AnchorTrendEnvelope:
    """The By-Question anchor's aggregate Bull/Bear trend, pre-computed into
    ts_page_cache by markets-board-snapshot. Two sources, whichever spans MORE:
    the backfilled 30d `markets-trend:*` (hourly detail for the 7/30D chips) and
    the forward-accruing `markets-anchor:*` scalar cache (up to 90d, cheap — lets
    the 90D chip light up as it accrues). Single read; the request path never
    scans ts_price_snapshots per contract."""
    recent_raw = await read_latest(session, f"markets-trend:{index_id}:{anchor}")
    recent = AnchorTrendPayload(**recent_raw) if recent_raw is not None else None
    scalar = await _anchor_scalar_trend(session, index_id, anchor)
    # Use the scalar cache only once it spans MORE than the backfilled 30d trend,
    # so the 90D chip lights up as it accrues with no near-term regression.
    best = recent
    if scalar is not None and (recent is None or scalar[1] > _RECENT_TREND_DAYS):
        best = scalar[0]
    if best is not None:
        return AnchorTrendEnvelope(summary="anchor trend (cached)", data=best)
    # Cold cache: compute just this anchor from the cached board (bounded — one
    # anchor's members, not the whole vertical), so we still avoid a live fold.
    board = await read_latest(session, f"markets-board:{index_id}")
    if board is not None:
        arows = [r for r in board.get("rows", []) if r.get("anchor") == anchor]
        if arows:
            trends = await compute_anchor_trends(session, arows, board.get("implied_spot"))
            t = trends.get(anchor)
            if t is not None:
                return AnchorTrendEnvelope(summary="anchor trend (live)", data=AnchorTrendPayload(**t))
    return AnchorTrendEnvelope(summary="no trend", data=AnchorTrendPayload())


async def _board_control_series(
    session: AsyncSession, index_id: str, cutoff: datetime | None, days: int = 14
) -> list[BoardTrendPoint]:
    """Hourly P(GOP control) history for a seat board, read from its slim
    snapshot history (one point per hour). Drives the board trend's hover
    tooltip; empty until snapshots accumulate. ``cutoff=None`` means "now" and
    is served from the short-TTL memo (every seat page asks for all three)."""
    rows = await board_history_points(
        session, index_id, ("p_r_control",), days, as_of=cutoff, bucket="hour"
    )
    return [BoardTrendPoint(date=d, value=v) for d, v, _ver in rows]


@router.get("/{index_id}/races/{seat_key}/trend", response_model=RaceTrendEnvelope)
async def race_trend(
    index_id: str,
    seat_key: str,
    days: int = Query(default=30, ge=1, le=90),
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> RaceTrendEnvelope:
    """One seat's P(R) over time, from the board cache's own history (the Seat
    page's chart — D10-lite). Senate/governor races key by 2-letter state
    (``prob_by_state``); house races by zero-padded district code (``AK-01`` →
    ``prob_by_district``, which the house board has always written). Reads the
    slim board history (pmi_core.engine.board_trend), not the full snapshots."""
    key = seat_key.upper()
    path = ("prob_by_district", key) if "-" in key else ("prob_by_state", key)
    rows = await board_history_points(session, index_id, path, days, as_of=as_of, bucket="hour")
    points = [BoardTrendPoint(date=d, value=v) for d, v, _ver in rows]
    return RaceTrendEnvelope(
        summary=f"{key}: {len(points)} hourly P(R) points over {days}d.",
        data=RaceTrendPayload(index_id=index_id, seat_key=key, days=days, points=points),
    )


def _apply_incumbency(payload: SenateBoardPayload, index_id: str) -> None:
    """Fill each race's incumbent_party from the static incumbency config — a
    serve-time enrichment, never a data-plane write, so it applies equally to the
    cached and live-computed payloads. Office + cycle come from the index id
    (us-senate-2026-republican-seats → office "senate", cycle 2026). Senate/governor
    key on the 2-letter state; house keys per-district — the board's race.state is a
    zero-padded district code (AK-01), which incumbent_party normalizes to the
    roster's AK-1, so house HELD BY/flips now resolve (roster compiled 2026-09-13)."""
    parts = index_id.split("-")
    if len(parts) < 3:
        return
    office = parts[1]
    try:
        cycle = int(parts[2])
    except ValueError:
        return
    for race in payload.races:
        if race.state:
            race.incumbent_party = incumbent_party(office, race.state, cycle)


@router.get("/{index_id}/senate-board", response_model=SenateBoardEnvelope)
async def senate_board(
    index_id: str,
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> SenateBoardEnvelope:
    """Balance-of-power board for a seat-projection index (SHIP-2.5).

    Fast path: reads from ts_page_cache (written by senate-board-snapshot worker
    every 10 min). Falls back to live computation when cache is empty or
    as_of is specified.
    """
    # ── Fast path: read from cache (no ts_price_snapshots scan) ─────────────
    if as_of is None:
        cached = await session.scalar(
            select(TsPageCache.payload)
            .where(TsPageCache.cache_key == f"senate-board:{index_id}")
            .order_by(TsPageCache.computed_at.desc())
            .limit(1)
        )
        if cached is not None:
            payload = SenateBoardPayload.model_validate(cached)
            payload.control_series = await _board_control_series(session, index_id, None)
            _apply_incumbency(payload, index_id)
            return SenateBoardEnvelope(
                summary=(
                    f"{payload.index_id} (v{payload.version}): "
                    f"P(R control) = {payload.p_r_control:.1f}%, "
                    f"E[R seats] = {payload.expected_r_seats:.1f}, "
                    f"{payload.n_contested} contested (cached)"
                ),
                data=payload,
            )

    # ── Slow path: live computation (as_of specified or cache empty) ─────────
    index_def = await _current_def(session, index_id)

    stmt = select(TsIndexScore).where(TsIndexScore.index_definition_id == index_def.id)
    if as_of is not None:
        stmt = stmt.where(TsIndexScore.as_of <= as_of)
    stmt = stmt.order_by(desc(TsIndexScore.as_of)).limit(1)
    score = (await session.execute(stmt)).scalar_one_or_none()
    if score is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NO_SCORE_YET", "hint": "Run the pipeline first."}},
        )

    # A balance-of-power board only means something for a seat-projection index.
    # Without this the chamber geometry silently falls back to _SEAT_DEFAULTS and
    # every other index answers 200 with nonsense — after spending minutes
    # collapsing thousands of unrelated components into "seats". Checked after
    # the score lookup so an unscored index still reports NO_SCORE_YET.
    formula = ((index_def.definition or {}).get("aggregation") or {}).get("formula")
    if formula != "seat_projection_sum":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "NOT_A_SEAT_BOARD",
                    "hint": (
                        f"{index_id} aggregates with '{formula}', not "
                        "'seat_projection_sum'. Try /indexes/{id}/explain."
                    ),
                }
            },
        )

    # Component markets = distinct markets behind the score's evaluations.
    # republican_on_yes rides along: it's the party hint for candidate-name
    # markets (Alaska has no party-direct market — see seat_mapping docstring).
    # seat_race_extraction rides along too: the audited LLM extraction
    # fallback for titles no pattern understands (lowest precedence tier).
    market_ids: list[int] = []
    rep_on_yes_by_market: dict[int, float] = {}
    extraction_by_market: dict[int, tuple[str, str]] = {}
    if score.component_evaluation_ids:
        eval_rows = (
            await session.execute(
                select(
                    AuditEvaluation.market_id,
                    AuditEvaluation.factor_id,
                    AuditEvaluation.value_numeric,
                    AuditEvaluation.value_label,
                ).where(AuditEvaluation.id.in_(score.component_evaluation_ids))
            )
        ).all()
        market_ids = sorted({mid for mid, _, _, _ in eval_rows})
        rep_on_yes_by_market = {
            mid: float(value)
            for mid, factor_id, value, _ in eval_rows
            if factor_id == "republican_on_yes" and value is not None
        }
        for mid, factor_id, value, label in eval_rows:
            if factor_id == "seat_race_extraction" and value == 1.0:
                ext = parse_extraction_label(label)
                if ext is not None:
                    extraction_by_market[mid] = ext

    title_by_id: dict[int, str] = {}
    venue_by_id: dict[int, str] = {}
    raw_by_id: dict[int, dict] = {}
    if market_ids:
        markets = (
            await session.execute(select(CoreMarket).where(CoreMarket.id.in_(market_ids)))
        ).scalars().all()
        title_by_id = {m.id: m.title for m in markets}
        venue_by_id = {m.id: m.venue for m in markets}
        raw_by_id = {m.id: m.raw for m in markets if isinstance(m.raw, dict)}

    # Latest price + volume at-or-before score.as_of, one row per market. The
    # LATERAL probe is one index seek per market; the DISTINCT ON it replaces
    # walked every (market, snapshot) entry ≤ as_of instead — see
    # markets_board_compute._freshest_snapshot_before.
    price_by_market: dict[int, float] = {}
    volume_by_market: dict[int, float | None] = {}
    if market_ids:
        for mid, (price, vol) in (
            await _freshest_snapshot_before(session, market_ids, score.as_of)
        ).items():
            if price is not None:
                price_by_market[mid] = price
                volume_by_market[mid] = vol

    # Chamber geometry from `aggregation.seat_projection` (CORR-1.2). Read the
    # raw definition dict (not the full IR) so the board never 500s on an
    # unrelated IR drift; fall back to the no-holdover Senate shape.
    agg = (index_def.definition or {}).get("aggregation") or {}
    sp = agg.get("seat_projection") or {}
    total_seats = int(sp.get("total_seats", _SEAT_DEFAULTS["total_seats"]))
    majority_threshold = int(
        sp.get("majority_threshold", _SEAT_DEFAULTS["majority_threshold"])
    )
    holdover_r = int(sp.get("holdover_r", _SEAT_DEFAULTS["holdover_r"]))
    holdover_d = int(sp.get("holdover_d", _SEAT_DEFAULTS["holdover_d"]))
    tie_break = sp.get("tie_break")
    correlation = sp.get("correlation")

    # CORR-1.3: collapse the broad component-market set into one probability
    # per contested seat. A "senate" keyword selector matches ~390 live markets
    # (nominee/primary, foreign senates, procedural, chamber brackets) — feeding
    # all of them to the Poisson-binomial gives E[R seats] > 100. seat_mapping
    # keeps party-direct per-state races ("Will the Republicans win the Ohio
    # Senate race in 2026?"), dedups R/D markets of the same state to a single
    # seat, and maps candidate-name markets ("Will Dan Sullivan win the Alaska
    # Senate race in 2026?") for the states that never get a party-direct
    # market — party resolved from Gamma's groupItemTitle tag when present,
    # else the audited republican_on_yes factor. Yields the 35 seats on the
    # 2026 ballot (fewer if not all have markets yet).
    ordered_ids = [mid for mid in market_ids if mid in price_by_market]
    party_hints = {
        mid: party
        for mid in ordered_ids
        if (
            party := resolve_candidate_party(
                (raw_by_id.get(mid) or {}).get("groupItemTitle"),
                rep_on_yes_by_market.get(mid),
            )
        )
        is not None
    }
    seats = extract_contested_seats(
        [(mid, title_by_id.get(mid, ""), price_by_market[mid]) for mid in ordered_ids],
        party_by_market=party_hints,
        venue_by_market={mid: venue_by_id.get(mid) for mid in ordered_ids},
        extraction_by_market=extraction_by_market,
    )
    contested_probs = [s.prob_r for s in seats]

    dist = compute_seat_distribution(
        contested_probs,
        holdover_r=holdover_r,
        holdover_d=holdover_d,
        total_seats=total_seats,
        majority_threshold=majority_threshold,
        tie_break=tie_break,
        correlation=correlation,
    )
    counts = band_counts(contested_probs, holdover_r=holdover_r, holdover_d=holdover_d)

    # Enrich one-sided seats with the complementary per-party market. The
    # seat formula's relevancy factor typically zeroes the D-side market out
    # of the score lineage (republican_on_yes = 0), so the components alone
    # never carry the Democratic candidate's name — but Polymarket names both
    # sides via groupItemTitle. One regex scan over unresolved markets fills
    # the missing half of each matchup.
    missing_sides = {
        (s.state, "R" if s.r_market_id is None else "D")
        for s in seats
        if s.r_market_id is None or s.d_market_id is None
    }
    complement_by_side: dict[tuple[str, str], CoreMarket] = {}
    if missing_sides:
        # SQL prefilter only — parse_seat_race below is the exact filter. One
        # alternation per venue phrasing (mirrors seat_mapping's patterns).
        comp_rows = (
            await session.execute(
                select(CoreMarket).where(
                    CoreMarket.title.op("~*")(
                        r"will the (republicans|democrats) win the .+ senate race in 2026"
                        r"|will the (republican|democratic) party win the .+ general election for us senate in 2026"
                        r"|which party will win the 2026 us senate election in"
                    ),
                    *live_market_conditions(),
                    CoreMarket.id.notin_(market_ids) if market_ids else True,
                )
            )
        ).scalars().all()
        # Same venue-priority order as the seat merge, so a polymarket
        # complement (with groupItemTitle candidate names) beats other venues.
        def _comp_rank(m: CoreMarket) -> int:
            try:
                return VENUE_PRIORITY.index(m.venue)
            except ValueError:
                return len(VENUE_PRIORITY)

        for m in sorted(comp_rows, key=_comp_rank):
            parsed = parse_seat_race(m.title)
            if parsed is None:
                continue
            party, state = parsed
            key = (state, party)
            if key in missing_sides and key not in complement_by_side:
                complement_by_side[key] = m

    # CORR-1.3 step-2 per-race attribution. delta_14d compares each seat's
    # P(R) against the snapshot at-or-before 14 days prior; seats whose
    # history doesn't reach back that far stay null (rendered as "—").
    prior_price_by_market: dict[int, float] = {}
    rep_ids = [s.market_id for s in seats]
    if rep_ids:
        prior_cutoff = score.as_of - timedelta(days=14)
        for mid, (price, _vol) in (
            await _freshest_snapshot_before(session, rep_ids, prior_cutoff)
        ).items():
            if price is not None:
                prior_price_by_market[mid] = price

    races = []
    for s in seats:
        comp_r = complement_by_side.get((s.state, "R"))
        comp_d = complement_by_side.get((s.state, "D"))
        side_markets: list[tuple[int, dict | None, str | None]] = []
        if s.r_market_id is not None:
            side_markets.append((s.r_market_id, raw_by_id.get(s.r_market_id), venue_by_id.get(s.r_market_id)))
        elif comp_r is not None:
            side_markets.append((comp_r.id, comp_r.raw if isinstance(comp_r.raw, dict) else None, comp_r.venue))
        else:
            side_markets.append((0, None, None))
        if s.d_market_id is not None:
            side_markets.append((s.d_market_id, raw_by_id.get(s.d_market_id), venue_by_id.get(s.d_market_id)))
        elif comp_d is not None:
            side_markets.append((comp_d.id, comp_d.raw if isinstance(comp_d.raw, dict) else None, comp_d.venue))
        else:
            side_markets.append((0, None, None))

        (_, r_raw, _), (_, d_raw, _) = side_markets
        r_label = _candidate_label(r_raw)
        d_label = _candidate_label(d_raw)
        matchup = (
            f"{r_label or 'Republican'} vs {d_label or 'Democrat'}"
            if (r_label or d_label)
            else None
        )
        source_ids = [mid for mid, _, _ in side_markets if mid]
        source_venues = sorted({v for _, _, v in side_markets if v})
        delta_14d = None
        prior_price = prior_price_by_market.get(s.market_id)
        if prior_price is not None:
            prior_prob_r = prior_price if s.source_party == "R" else 1.0 - prior_price
            prior_prob_r = max(0.0, min(1.0, prior_prob_r))
            delta_14d = round((s.prob_r - prior_prob_r) * 100.0, 2)
        races.append(
            SenateRace(
                market_id=s.market_id,
                title=title_by_id.get(s.market_id, "(unknown)"),
                prob_r=round(s.prob_r * 100.0, 2),
                band=classify_band(s.prob_r),
                volume_24h=volume_by_market.get(s.market_id),
                state=s.state_code,
                matchup=matchup,
                delta_14d=delta_14d,
                contracts=len(source_ids),
                exchanges=source_venues,
            )
        )
    prob_by_state = {
        s.state_code: round(s.prob_r * 100.0, 2)
        for s in seats
        if s.state_code is not None
    }

    # series_14d: the chronological tail of stored scores (≤ score.as_of).
    hist_rows = (
        await session.execute(
            select(TsIndexScore.score)
            .where(
                TsIndexScore.index_definition_id == index_def.id,
                TsIndexScore.as_of <= score.as_of,
            )
            .order_by(TsIndexScore.as_of.desc())
            .limit(14)
        )
    ).scalars().all()
    series_14d = [float(s) if s is not None else None for s in reversed(hist_rows)]

    d_secured = counts["safe-d"] + counts["likely-d"] + counts["lean-d"]
    r_secured = counts["safe-r"] + counts["likely-r"] + counts["lean-r"]

    payload = SenateBoardPayload(
        index_id=index_def.index_id,
        version=index_def.version,
        as_of=score.as_of,
        p_r_majority=round(dist.p_r_majority * 100.0, 2),
        p_d_majority=round(dist.p_d_majority * 100.0, 2),
        p_r_control=round(dist.p_r_control * 100.0, 2),
        p_d_control=round(dist.p_d_control * 100.0, 2),
        tie_break=tie_break,
        correlation=float(correlation) if correlation is not None else None,
        expected_r_seats=round(dist.expected_r_seats, 2),
        stdev_r_seats=round(dist.stdev_r_seats, 2),
        total_seats=total_seats,
        majority_threshold=majority_threshold,
        holdover_r=holdover_r,
        holdover_d=holdover_d,
        counts=counts,
        d_secured=d_secured,
        r_secured=r_secured,
        tossups=counts["tossup"],
        n_contested=dist.n_contested,
        races=races,
        prob_by_state=prob_by_state,
        series_14d=series_14d,
        control_series=await _board_control_series(session, index_id, score.as_of),
    )
    _apply_incumbency(payload, index_id)
    return SenateBoardEnvelope(
        summary=(
            f"{index_def.title} (v{index_def.version}): "
            f"P(R control) = {payload.p_r_control:.1f}%, "
            f"P(D control) = {payload.p_d_control:.1f}%, "
            f"E[R seats] = {payload.expected_r_seats:.1f} "
            f"across {dist.n_contested} contested + "
            f"{holdover_r + holdover_d} holdover, as of {score.as_of.isoformat()}"
        ),
        data=payload,
    )
