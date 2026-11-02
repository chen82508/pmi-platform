"""`reeval-drifted` job — Tier 3 periodic re-evaluation trigger (CORR-5.2).

CLAUDE.md §6: "不每天全表跑；用 (價格漂移 > X%) 觸發". Instead of waiting for
the hourly cron, this job re-scores exactly the indexes whose latest score
contains a component market whose price drifted ≥ `PMI_DRIFT_THRESHOLD_PCT`
probability points since that score was computed.

What "re-evaluation" means here: factor evaluations are append-only and keyed
by (market, index_def, factor, prompt, model) — semantic verdicts like "is this
about war" don't change because a price moved, so the cache stays valid. What
DOES go stale on a price swing is the *score*: component prices, liquidity
weights, collapse representatives. A pipeline tick refreshes all of those.
Run it on a tighter cadence than the hourly `score-all` (e.g. every 10 min via
cron); when nothing drifted it's a cheap no-op.

Drift detection
---------------
Per component market of each current index's latest score row:
  p_now  = latest ts_price_snapshots.last_price
  p_then = latest snapshot at or before the baseline
  drift  = |p_now − p_then| × 100   (probability points)
Markets with no snapshot at or before the baseline are skipped — a brand-new
market has nothing to compare against.

The baseline is **the score's own `as_of`**, not a fixed window: drift means
"moved since the number we're about to replace". Measuring against a fixed
`now − PMI_DRIFT_LOOKBACK_HOURS` instead made every re-score a no-op in the
sense that mattered — a market that jumped 10 points once stayed over the
threshold for the whole lookback, so its index was re-scored on every single
beat for the next 24 hours. Two of those full passes then sat on the batch
lane for hours at a time and the hourly `score-all` never got the GPU, which
is how half the board indexes ended up ~17 hours stale while the boards
themselves were re-cached every 10 minutes. Anchoring to `as_of` makes the
drift reset the moment we act on it. `PMI_DRIFT_LOOKBACK_HOURS` survives as a
floor: never compare against a baseline older than that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    TsIndexScore,
    TsPriceSnapshot,
)
from sqlalchemy import BigInteger, func, literal, select, true
from sqlalchemy.dialects.postgresql import ARRAY

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.reeval_drifted")


async def _latest_score_components(
    session, index_def_ids: list[int]
) -> dict[int, tuple[datetime, set[int]]]:
    """Map index_definition_id → (latest score's as_of, its component market ids)."""
    out: dict[int, tuple[datetime, set[int]]] = {}
    for def_id in index_def_ids:
        score = (
            await session.execute(
                select(TsIndexScore)
                .where(TsIndexScore.index_definition_id == def_id)
                .order_by(TsIndexScore.as_of.desc())
                .limit(1)
            )
        ).scalars().first()
        if score is None or not score.component_evaluation_ids:
            continue
        rows = (
            await session.execute(
                select(AuditEvaluation.market_id).where(
                    AuditEvaluation.id.in_(score.component_evaluation_ids)
                )
            )
        ).scalars().all()
        out[def_id] = (score.as_of, set(rows))
    return out


async def _drifted_markets(
    session, market_ids: set[int], baseline: datetime
) -> dict[int, float]:
    """Return {market_id: drift_points} for markets that moved ≥ threshold
    since `baseline`."""
    if not market_ids:
        return {}
    threshold = settings.drift_threshold_pct

    # Two LATERAL ... LIMIT 1 probes per market (same shape as
    # pipeline._latest_prices): the freshest priced snapshot, and the freshest
    # priced snapshot at-or-before the baseline. The old plan (`market_id IN (…)
    # ORDER BY … DESC` with no LIMIT, twice) streamed the ENTIRE price history
    # of every component market to Python every 15 minutes — millions of rows
    # per run against the ~300M-row prod table — then kept one row per market.
    # INNER joins drop markets missing either side, preserving the "no p_then →
    # no drift baseline → skip" semantics.
    mids = (
        func.unnest(literal(list(market_ids), type_=ARRAY(BigInteger)))
        .table_valued("id")
        .render_derived(with_types=False)
    )
    now_l = (
        select(TsPriceSnapshot.last_price)
        .where(
            TsPriceSnapshot.market_id == mids.c.id,
            TsPriceSnapshot.last_price.is_not(None),
        )
        .order_by(TsPriceSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("p_now")
    )
    then_l = (
        select(TsPriceSnapshot.last_price)
        .where(
            TsPriceSnapshot.market_id == mids.c.id,
            TsPriceSnapshot.last_price.is_not(None),
            TsPriceSnapshot.snapshot_at <= baseline,
        )
        .order_by(TsPriceSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("p_then")
    )
    rows = (
        await session.execute(
            select(mids.c.id, now_l.c.last_price, then_l.c.last_price).select_from(
                mids.join(now_l, true()).join(then_l, true())
            )
        )
    ).all()

    drifted: dict[int, float] = {}
    for mid, price_now, price_then in rows:
        drift = abs(float(price_now) - float(price_then)) * 100.0
        if drift >= threshold:
            drifted[mid] = round(drift, 2)
    return drifted


@register("reeval-drifted", queue="batch")
async def run() -> None:
    from pmi_core.engine import run_pipeline

    log.info(
        "reeval_drifted.start",
        threshold_pct=settings.drift_threshold_pct,
        max_lookback_hours=settings.drift_lookback_hours,
    )
    async with session_scope() as session:
        defs = (
            await session.execute(
                select(CoreIndexDefinition.id, CoreIndexDefinition.index_id).where(
                    CoreIndexDefinition.is_current.is_(True)
                )
            )
        ).all()
        def_ids = [d.id for d in defs]
        id_to_slug = {d.id: d.index_id for d in defs}
        components = await _latest_score_components(session, def_ids)

        floor = datetime.now(UTC) - timedelta(hours=settings.drift_lookback_hours)
        to_rescore: dict[str, dict[int, float]] = {}
        for def_id, (as_of, market_ids) in components.items():
            drifted = await _drifted_markets(session, market_ids, max(as_of, floor))
            if drifted:
                to_rescore[id_to_slug[def_id]] = drifted

    if not to_rescore:
        log.info("reeval_drifted.no_drift")
        return

    for index_id, drifted in to_rescore.items():
        top = sorted(drifted.items(), key=lambda kv: kv[1], reverse=True)[:5]
        log.info(
            "reeval_drifted.rescoring",
            index_id=index_id,
            drifted_markets=len(drifted),
            top_drifts=top,
        )
        try:
            result = await run_pipeline(index_id=index_id)
            log.info(
                "reeval_drifted.rescored",
                index_id=index_id,
                score=result.get("score"),
            )
        except Exception as exc:  # noqa: BLE001 - one index failing ≠ job dead
            log.error("reeval_drifted.failed", index_id=index_id, error=str(exc)[:200])

    log.info("reeval_drifted.done", indexes_rescored=len(to_rescore))
