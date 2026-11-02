"""GDP board + per-period compute — shared by the gdp-board-snapshot worker
(writes ts_page_cache) and the pmi-api /gdp cache-miss fallback.

The /gdp/period page used to recompute build_board AND reconstruct the per-hour
trend (re-scoring build_board once per hour off ts_price_snapshots) on every
request — ~6s. That heavy work now runs once in the snapshot; the route reads a
pre-computed blob. Returns plain JSON-serialisable dicts (no pmi-api imports).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Select, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.engine import gdp_board as gb
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.models import CoreMarket, TsPriceSnapshot

GDP_INDEX_ID = "us-gdp-expectation"


async def _load_rows(session: AsyncSession) -> list:
    """Live US-GDP-growth market rows (shared live gate + gdp_board term prefilter)."""
    stmt = select(
        CoreMarket.id,
        CoreMarket.venue,
        CoreMarket.title,
        CoreMarket.last_price,
        CoreMarket.last_volume_24h,
        CoreMarket.last_price_at,
        CoreMarket.url,
    ).where(
        *live_market_conditions(),
        CoreMarket.last_price.isnot(None),
        CoreMarket.venue.notin_(list(gb.EXCLUDE_VENUES)),
        *(CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REQUIRE_ALL),
        or_(*(CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REQUIRE_ANY)),
        *(~CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REJECT_ANY),
    )
    return (await session.execute(stmt)).all()


def _members(rows: list) -> list[gb.Member]:
    return [
        gb.Member(
            market_id=r.id,
            venue=r.venue,
            title=r.title,
            price=float(r.last_price) if r.last_price is not None else None,
            volume_24h=float(r.last_volume_24h) if r.last_volume_24h is not None else None,
            price_at=r.last_price_at,
        )
        for r in rows
    ]


_HOURLY_TAIL_DAYS = 14  # fine (hourly) resolution only across the recent tail


def seed_snapshot_stmt(market_ids: list[int], start: datetime) -> Select:
    """Each market's freshest snapshot strictly before `start` — one LATERAL ...
    LIMIT 1 index probe per market (the `_freshest_snapshot_before` shape from
    markets_board_compute). The DISTINCT ON it replaces walked EVERY index entry
    of every member before `start` — months of 5-min snapshots on the ~210 GB
    table, 70-140 s per period, 16 periods per gdp-board-snapshot (2026-10-20);
    this probes 33 markets in ~130 ms. Markets with no snapshot are absent, as
    before."""
    ids = (
        func.unnest(literal(market_ids, type_=ARRAY(BigInteger)))
        .table_valued("id")
        .render_derived(with_types=False)
    )
    latest = (
        select(TsPriceSnapshot.snapshot_at, TsPriceSnapshot.last_price)
        .where(TsPriceSnapshot.market_id == ids.c.id, TsPriceSnapshot.snapshot_at < start)
        .order_by(TsPriceSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("ps")
    )
    return select(ids.c.id, latest.c.snapshot_at, latest.c.last_price).select_from(
        ids.join(latest, true())
    )


async def _period_series(
    session: AsyncSession, meta: dict[int, tuple[str, str, float]], days: int = 14
) -> list[dict]:
    """Hourly sentiment for ONE period, reconstructed from ts_price_snapshots
    (forward-filled per hour, re-scored through build_board). Last point at `now`
    so it lands on the headline tile. (Moved verbatim from routes/gdp.py.)"""
    if not meta:
        return []
    cutoff = datetime.now(UTC)
    start = cutoff - timedelta(days=days)
    ts_arr: dict[int, list] = defaultdict(list)
    pr_arr: dict[int, list] = defaultdict(list)

    seed = (await session.execute(seed_snapshot_stmt(list(meta), start))).all()
    for mid, ts, pr in seed:
        if pr is not None:
            ts_arr[mid].append(ts)
            pr_arr[mid].append(float(pr))

    rows = (
        await session.execute(
            select(TsPriceSnapshot.market_id, TsPriceSnapshot.snapshot_at, TsPriceSnapshot.last_price)
            .where(TsPriceSnapshot.market_id.in_(list(meta)), TsPriceSnapshot.snapshot_at >= start)
            .order_by(TsPriceSnapshot.market_id, TsPriceSnapshot.snapshot_at)
        )
    ).all()
    for mid, ts, pr in rows:
        if pr is not None:
            ts_arr[mid].append(ts)
            pr_arr[mid].append(float(pr))

    def _score_at(t: datetime) -> float | None:
        members = []
        for mid, (venue, title, vol) in meta.items():
            ta = ts_arr.get(mid)
            if not ta:
                continue
            idx = bisect.bisect_right(ta, t) - 1
            if idx >= 0:
                members.append(gb.Member(mid, venue, title, pr_arr[mid][idx], vol))
        return gb.build_board(members, index_id=GDP_INDEX_ID)["score"]

    # Adaptive resolution so a 90d window stays cheap and light: hourly across the
    # recent tail (the 7/30D range chips need fine detail), daily further back (the
    # 90D chip only needs the shape). ~412 points/period at 90d vs ~2160 naive —
    # keeps the batch-lane snapshot inside its 10-min cadence.
    hourly_from = cutoff - timedelta(days=_HOURLY_TAIL_DAYS)
    out: list[dict] = []
    h = start.replace(minute=0, second=0, microsecond=0)
    last = None
    while h <= cutoff:
        v = _score_at(h)
        if v is not None:
            out.append({"date": h.isoformat(), "value": v})
        last = h
        h += timedelta(hours=1) if h >= hourly_from else timedelta(hours=24)
    if last is None or last < cutoff:
        v = _score_at(cutoff)
        if v is not None:
            out.append({"date": cutoff.isoformat(), "value": v})
    return out


def _period_contracts(rows: list, period: str) -> tuple[list[dict], dict[int, tuple[str, str, float]]]:
    contracts: list[dict] = []
    meta: dict[int, tuple[str, str, float]] = {}
    for r in rows:
        if not gb.is_us(r.title):
            continue
        a = gb.anchor_of(r.title)
        if a is None or a[1] != period:
            continue
        reading = gb.parse_reading(r.title)
        if reading is None:
            continue
        meta[r.id] = (r.venue, r.title, float(r.last_volume_24h or 0))
        contracts.append(
            {
                "market_id": r.id,
                "title": r.title,
                "venue": r.venue,
                "price": round(float(r.last_price) * 100.0, 1),
                "kind": reading[0],
                "value": reading[1],
                "volume_24h": float(r.last_volume_24h) if r.last_volume_24h is not None else None,
                "url": r.url,
            }
        )
    contracts.sort(key=lambda c: (c["value"] is None, c["value"] or 0.0))
    return contracts, meta


async def compute_gdp(session: AsyncSession) -> tuple[dict, dict[str, dict]]:
    """(board_payload, {period: {anchor, contracts, series}}) — the whole GDP
    vertical pre-computed once. `period_detail` feeds /gdp/period."""
    rows = await _load_rows(session)
    board = gb.build_board(_members(rows), index_id=GDP_INDEX_ID)
    board["as_of"] = datetime.now(UTC).isoformat()

    periods: dict[str, dict] = {}
    for anchor in board.get("anchors", []):
        period = anchor["period"]
        contracts, meta = _period_contracts(rows, period)
        periods[period] = {
            "period": period,
            "anchor": anchor,
            "contracts": contracts,
            "series": await _period_series(session, meta),
        }
    return board, periods
