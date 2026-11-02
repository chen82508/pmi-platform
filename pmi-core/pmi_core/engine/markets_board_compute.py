"""Shared computation for the /micah/markets board.

Extracted from `pmi_api.routes.indexes.markets_board` so it can run in TWO
places against the SAME main `pmi` DB:

  1. the markets-board-snapshot worker (pmi-workers), which pre-computes the
     board into `ts_page_cache` every 10 min — exactly like war/senate/gdp; and
  2. the pmi-api route, as the `as_of=` slow path / cache-miss fallback.

History: the board used to live-compute on every request against a separate
static `pmi_btc` snapshot DB, because live-computing over the ~1M-row main `pmi`
times out. That DB was ephemeral and vanished during an EC2 rebuild (→ 500s).
Moving the heavy compute onto the 10-min snapshot cadence removes the second DB:
BTC now serves from cache off the main `pmi`, matching every other vertical.

Returns a plain JSON-serialisable dict (the `MarketsBoardPayload` shape) so the
worker can write it straight to `ts_page_cache` and pmi-api can
`MarketsBoardPayload.model_validate(...)` it back. Kept free of any pmi-api
import so pmi-workers (which only depends on pmi-core) can call it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import BigInteger, desc, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.dsl.ir import IndexDef
from pmi_core.engine import markets_board as mb
from pmi_core.engine.aggregator import _direction_value, _relevancy
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.engine.moneyness import extract_strike
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    TsIndexScore,
    TsPriceSnapshot,
)

_SPARK_BINS = 12

# Minimum contracts priced in BOTH the current and the 24h-ago snapshot before a
# same-basket repricing delta is meaningful (CORR-1.14); thinner → None.
_MIN_SAME_BASKET = 4

_VENUE_DISPLAY = {
    "kalshi": "Kalshi",
    "forecastex": "ForecastEx",
    "gemini": "Gemini",
    "polymarket": "Polymarket",
    "predictit": "PredictIt",
    "manifold": "Manifold",
}


async def _current_def(session: AsyncSession, index_id: str) -> CoreIndexDefinition | None:
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
        # Defensive: fall back to highest version if is_current was never set.
        row = (
            await session.execute(
                select(CoreIndexDefinition)
                .where(CoreIndexDefinition.index_id == index_id)
                .order_by(CoreIndexDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row


def _mid_values(market_ids: list[int]):
    """`unnest(:ids) AS anon(id)` — the driving side of the LATERAL probes."""
    return (
        func.unnest(literal(market_ids, type_=ARRAY(BigInteger)))
        .table_valued("id")
        .render_derived(with_types=False)
    )


async def _freshest_snapshot_before(
    session: AsyncSession,
    market_ids: list[int],
    cutoff: datetime,
    *,
    priced_only: bool = False,
) -> dict[int, tuple[float | None, float | None]]:
    """{market_id: (last_price, volume_24h)} from each market's freshest
    snapshot at-or-before `cutoff` — one LATERAL ... LIMIT 1 index probe per
    market (same shape as pipeline._latest_prices). The DISTINCT ON queries
    this replaces walked EVERY (market, snapshot) index entry ≤ cutoff — for
    months-old components that is millions of entries per 10-min snapshot run
    against the ~300M-row prod table. Markets with no qualifying snapshot are
    simply absent."""
    if not market_ids:
        return {}
    mids = _mid_values(market_ids)
    conds = [
        TsPriceSnapshot.market_id == mids.c.id,
        TsPriceSnapshot.snapshot_at <= cutoff,
    ]
    if priced_only:
        conds.append(TsPriceSnapshot.last_price.is_not(None))
    latest = (
        select(TsPriceSnapshot.last_price, TsPriceSnapshot.volume_24h)
        .where(*conds)
        .order_by(TsPriceSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("ps")
    )
    stmt = select(mids.c.id, latest.c.last_price, latest.c.volume_24h).select_from(
        mids.join(latest, true())
    )
    return {
        mid: (
            float(price) if price is not None else None,
            float(vol) if vol is not None else None,
        )
        for mid, price, vol in (await session.execute(stmt)).all()
    }


async def compute_markets_board(
    session: AsyncSession,
    index_id: str,
    as_of: datetime | None = None,
    spot_override: float | None = None,
) -> dict | None:
    """Board payload for /micah/markets: score + regime, driver buckets
    (moneyness/kind grouping of the component set), composition weights,
    cross-version score series and the top holdings rows.

    Returns None when the index is unknown or has no score yet (caller decides:
    the route 404s, the snapshot worker logs + skips). Datetimes are serialised
    to ISO strings so the result is JSONB-ready for ts_page_cache.
    """
    index_def = await _current_def(session, index_id)
    if index_def is None:
        return None

    stmt = select(TsIndexScore).where(TsIndexScore.index_definition_id == index_def.id)
    if as_of is not None:
        stmt = stmt.where(TsIndexScore.as_of <= as_of)
    stmt = stmt.order_by(desc(TsIndexScore.as_of)).limit(1)
    score = (await session.execute(stmt)).scalar_one_or_none()
    if score is None:
        return None

    ir = IndexDef.model_validate(index_def.definition)
    # Asset name for anchor/driver labels — "BTC" vs "S&P 500", never hard-coded
    # (this board serves every bull/bear index, not just BTC).
    asset = mb.asset_label(index_id, getattr(ir, "title", None))
    score_val = float(score.score) if score.score is not None else None

    # ---- components: same resolution chain as /explain -------------------
    evals = (
        await session.execute(
            select(AuditEvaluation).where(
                AuditEvaluation.id.in_(score.component_evaluation_ids or [])
            )
        )
    ).scalars().all()
    by_market_evals: dict[int, dict[str, AuditEvaluation]] = {}
    for e in evals:
        by_market_evals.setdefault(e.market_id, {})[e.factor_id] = e
    market_ids = list(by_market_evals.keys())

    markets = (
        (await session.execute(select(CoreMarket).where(CoreMarket.id.in_(market_ids)))).scalars().all()
        if market_ids
        else []
    )
    market_by_id = {m.id: m for m in markets}

    last_price_by_market: dict[int, float | None] = {}
    volume_by_market: dict[int, float | None] = {}
    if market_ids:
        for mid, (price, vol) in (
            await _freshest_snapshot_before(session, market_ids, score.as_of)
        ).items():
            last_price_by_market[mid] = price
            volume_by_market[mid] = vol
        # Illiquid tail: a market whose latest snapshot carries a null price
        # falls back to its last KNOWN price. A targeted backscan over ONLY those
        # few markets keeps the common (fully-priced) fast path untouched.
        null_mids = [m for m in market_ids if last_price_by_market.get(m) is None]
        if null_mids:
            for mid, (price, vol) in (
                await _freshest_snapshot_before(
                    session, null_mids, score.as_of, priced_only=True
                )
            ).items():
                last_price_by_market[mid] = price
                if volume_by_market.get(mid) is None and vol is not None:
                    volume_by_market[mid] = vol

    # ---- implied spot: external exchange spot (from the snapshot worker) wins,
    # else the score's own ladder-implied spot. The override is display-only (it
    # sets the board payload's spot for anchor readings/trends); the index SCORE
    # still uses the ladder-implied moneyness kernel, so scoring stays
    # feed-independent. None everywhere → distribution anchors show "—".
    breakdown = score.breakdown or {}
    implied_spot = (breakdown.get("moneyness_weighting") or {}).get("implied_spot")
    implied_spot = float(implied_spot) if implied_spot else None
    if spot_override is not None:
        implied_spot = spot_override

    # ---- group components into driver buckets -----------------------------
    members_by_driver: dict[str, list[mb.Member]] = {}
    driver_by_market: dict[int, str] = {}
    relevancy_by_market: dict[int, float] = {}
    direction_by_market: dict[int, float] = {}
    for mid in market_ids:
        m = market_by_id.get(mid)
        key = mb.classify(m.title if m else None, implied_spot)
        driver_by_market[mid] = key
        rel = _relevancy(by_market_evals[mid], ir)
        drn = _direction_value(by_market_evals[mid], ir, m.title if m else None)
        relevancy_by_market[mid] = rel
        direction_by_market[mid] = drn
        members_by_driver.setdefault(key, []).append(
            mb.Member(
                market_id=mid,
                relevancy=rel,
                direction=drn,
                price=last_price_by_market.get(mid),
            )
        )

    total_relevancy = sum(relevancy_by_market.values()) or 1.0

    # ---- sparklines: time-bin the real snapshot window --------------------
    # The old plan streamed EVERY snapshot row of every component ≤ as_of
    # (months × 10-min cadence × N markets — millions of rows per run) just to
    # carry the last-known price into 12 bins. The carry-forward semantics are
    # exactly "freshest priced snapshot ≤ bin_end", so probe that directly:
    # one LATERAL per (market × bin) — members×12 index probes total.
    spark_by_driver: dict[str, list[float | None] | None] = {}
    window_start: datetime | None = None
    if market_ids:
        mids = _mid_values(market_ids)
        first_l = (
            select(TsPriceSnapshot.snapshot_at)
            .where(
                TsPriceSnapshot.market_id == mids.c.id,
                TsPriceSnapshot.snapshot_at <= score.as_of,
            )
            .order_by(TsPriceSnapshot.snapshot_at.asc())
            .limit(1)
            .lateral("first_snap")
        )
        window_start = (
            await session.execute(
                select(func.min(first_l.c.snapshot_at)).select_from(
                    mids.join(first_l, true())
                )
            )
        ).scalar()
    if window_start is not None:
        window = (score.as_of - window_start).total_seconds()
        if window > 0:
            bin_ends = [
                window_start + timedelta(seconds=window * (i + 1) / _SPARK_BINS)
                for i in range(_SPARK_BINS)
            ]
            mids = _mid_values(market_ids)
            bins = (
                func.unnest(literal(bin_ends, type_=ARRAY(TIMESTAMP(timezone=True))))
                .table_valued("bin_end", with_ordinality="ord")
                .render_derived(with_types=False)
            )
            binned_l = (
                select(TsPriceSnapshot.last_price)
                .where(
                    TsPriceSnapshot.market_id == mids.c.id,
                    TsPriceSnapshot.snapshot_at <= bins.c.bin_end,
                    TsPriceSnapshot.last_price.is_not(None),
                )
                .order_by(TsPriceSnapshot.snapshot_at.desc())
                .limit(1)
                .lateral("pb")
            )
            price_rows = (
                await session.execute(
                    select(mids.c.id, bins.c.ord, binned_l.c.last_price).select_from(
                        mids.join(bins, true()).join(binned_l, true())
                    )
                )
            ).all()
            price_by_bin: list[dict[int, float]] = [{} for _ in range(_SPARK_BINS)]
            for mid, ord_, p in price_rows:
                price_by_bin[int(ord_) - 1][mid] = float(p)
            series_by_driver: dict[str, list[float | None]] = {k: [] for k in members_by_driver}
            for prices in price_by_bin:
                for key, members in members_by_driver.items():
                    binned = [
                        mb.Member(
                            market_id=m.market_id,
                            relevancy=m.relevancy,
                            direction=m.direction,
                            price=prices.get(m.market_id),
                        )
                        for m in members
                    ]
                    series_by_driver[key].append(mb.signed_reading(binned))
            for key, series in series_by_driver.items():
                n_data = sum(1 for v in series if v is not None)
                spark_by_driver[key] = series if n_data >= 2 else None

    # ---- Δ14d: reading at prices as of (as_of − 14d); None-safe -----------
    old_price_by_market: dict[int, float] = {}
    if market_ids:
        cutoff_14d = score.as_of - timedelta(days=14)
        for mid, (price, _vol) in (
            await _freshest_snapshot_before(session, market_ids, cutoff_14d)
        ).items():
            if price is not None:
                old_price_by_market[mid] = price

    # ---- same-basket 24h repricing (CORR-1.14) ----------------------------
    # The homepage 24h card diffed two whole-index scores 24h apart, so a daily
    # strike-ladder roll (S&P/BTC "close above X on <today>" legs rotating out,
    # fresh ones in) read as a double-digit "move" on a flat market. Diff only
    # the contracts priced in BOTH snapshots, at the whole-basket level — one
    # extra batched probe, same shape as Δ14d above. basket_turnover_24h is the
    # share of the current priced basket that had no price 24h ago (new legs).
    now_price_by_market = {
        mid: p for mid in market_ids if (p := last_price_by_market.get(mid)) is not None
    }
    price_24h_by_market: dict[int, float] = {}
    if market_ids:
        for mid, (price, _vol) in (
            await _freshest_snapshot_before(
                session, market_ids, score.as_of - timedelta(hours=24), priced_only=True
            )
        ).items():
            if price is not None:
                price_24h_by_market[mid] = price
    priced_both = [mid for mid in now_price_by_market if mid in price_24h_by_market]

    def _basket_member(mid: int, price: float) -> mb.Member:
        return mb.Member(
            market_id=mid,
            relevancy=relevancy_by_market[mid],
            direction=direction_by_market[mid],
            price=price,
        )

    delta_24h_same_basket: float | None = None
    if len(priced_both) >= _MIN_SAME_BASKET:
        now_reading = mb.signed_reading(
            [_basket_member(mid, now_price_by_market[mid]) for mid in priced_both]
        )
        old_reading = mb.signed_reading(
            [_basket_member(mid, price_24h_by_market[mid]) for mid in priced_both]
        )
        if now_reading is not None and old_reading is not None:
            delta_24h_same_basket = round(now_reading - old_reading, 2)
    basket_turnover_24h: float | None = (
        round(1.0 - len(priced_both) / len(now_price_by_market), 4)
        if now_price_by_market
        else None
    )

    # ---- assemble drivers in canonical order ------------------------------
    drivers: list[dict] = []
    for key in mb.DRIVER_ORDER:
        members = members_by_driver.get(key)
        if not members:
            continue
        value = mb.signed_reading(members)
        delta_14d: float | None = None
        if value is not None and old_price_by_market:
            old_members = [
                mb.Member(
                    market_id=m.market_id,
                    relevancy=m.relevancy,
                    direction=m.direction,
                    price=old_price_by_market.get(m.market_id),
                )
                for m in members
            ]
            old_value = mb.signed_reading(old_members)
            if old_value is not None:
                delta_14d = round(value - old_value, 2)

        strikes = [
            s
            for s in (
                extract_strike(market_by_id[m.market_id].title)
                for m in members
                if m.market_id in market_by_id
            )
            if s is not None
        ]
        closes = sorted(
            {
                market_by_id[m.market_id].closes_at.date()
                for m in members
                if m.market_id in market_by_id and market_by_id[m.market_id].closes_at is not None
            }
        )
        sub_bits: list[str] = []
        if strikes:
            lo, hi = min(strikes), max(strikes)
            sub_bits.append(
                mb.fmt_strike(lo) if lo == hi else f"{mb.fmt_strike(lo)}–{mb.fmt_strike(hi)}"
            )
        if closes:
            first, last = closes[0], closes[-1]
            span = (
                first.strftime("%b %-d")
                if first == last
                else (
                    f"{first.strftime('%b %-d')}–"
                    f"{last.strftime('%-d') if first.month == last.month else last.strftime('%b %-d')}"
                )
            )
            sub_bits.append(f"closes {span}")
        drivers.append(
            {
                "key": key,
                "name": mb.driver_name(key, asset),
                "sub": " · ".join(sub_bits) or None,
                "value": value,
                "delta_14d": delta_14d,
                "weight_pct": round(
                    sum(m.relevancy for m in members) / total_relevancy * 100.0, 2
                ),
                "n_markets": len(members),
                "spark": spark_by_driver.get(key),
            }
        )

    # ---- stat rail: index-specific live universe --------------------------
    live_stmt = select(func.count(CoreMarket.id)).where(*live_market_conditions())
    if ir.venues:
        live_stmt = live_stmt.where(CoreMarket.venue.in_(ir.venues))
    kw_terms = [
        t
        for sel in ir.selectors
        if getattr(sel, "type", None) == "keyword"
        for t in getattr(sel, "terms", [])
    ]
    if kw_terms:
        live_stmt = live_stmt.where(or_(*[CoreMarket.title.ilike(f"%{t}%") for t in kw_terms]))
    for term in ir.exclude_terms or []:
        live_stmt = live_stmt.where(~CoreMarket.title.ilike(f"%{term}%"))
    if ir.min_volume_24h:
        live_stmt = live_stmt.where(CoreMarket.last_volume_24h >= ir.min_volume_24h)
    live_contracts = (await session.execute(live_stmt)).scalar_one()

    venue_counts: dict[str, int] = {}
    for m in markets:
        if m.venue:
            venue_counts[m.venue] = venue_counts.get(m.venue, 0) + 1
    exchanges = sorted(venue_counts, key=lambda v: venue_counts[v], reverse=True)

    # ---- cross-version score series ---------------------------------------
    # Column select, not the entity: every row's component_evaluation_ids array
    # and breakdown JSON are TOASTed, and reading them for the full history made
    # this an 11 s cold read for BTC instead of ~10 ms.
    series_rows = (
        await session.execute(
            select(
                TsIndexScore.as_of,
                TsIndexScore.score,
                TsIndexScore.component_count,
                CoreIndexDefinition.version,
            )
            .join(
                CoreIndexDefinition,
                CoreIndexDefinition.id == TsIndexScore.index_definition_id,
            )
            .where(CoreIndexDefinition.index_id == index_id)
            .order_by(TsIndexScore.as_of)
        )
    ).all()
    series = [
        {
            "as_of": s.as_of.isoformat(),
            "score": float(s.score) if s.score is not None else None,
            "version": s.version,
            "component_count": s.component_count,
        }
        for s in series_rows
    ]

    # ---- holdings rows: full component set, ranked ------------------------
    group_sizes = {k: len(v) for k, v in members_by_driver.items()}
    ranked = sorted(
        market_ids,
        key=lambda mid: (
            -relevancy_by_market.get(mid, 0.0),
            -(volume_by_market.get(mid) or 0.0),
        ),
    )
    rows: list[dict] = []
    for mid in ranked:
        m = market_by_id.get(mid)
        if m is None:
            continue
        price = last_price_by_market.get(mid)
        temp = mb.signed_reading(
            [
                mb.Member(
                    market_id=mid,
                    relevancy=relevancy_by_market.get(mid, 0.0) or 1.0,
                    direction=direction_by_market.get(mid, 0.0),
                    price=price,
                )
            ]
        )
        key = driver_by_market[mid]
        venue_disp = _VENUE_DISPLAY.get(m.venue or "", (m.venue or "").title())
        anchor_key, anchor_label = mb.anchor_of(m.venue, m.external_id, m.raw, m.title, asset=asset)
        rows.append(
            {
                "market_id": mid,
                "title": m.title,
                "prob": round(price * 100.0, 1) if price is not None else None,
                "temp": temp,
                "venues": [m.venue] if m.venue else [],
                "tags": [mb.driver_name(key, asset), venue_disp],
                "bucket": key,
                "anchor": anchor_key,
                "anchor_label": anchor_label,
                "contracts": group_sizes.get(key, 1),
                "volume_24h": volume_by_market.get(mid),
                "url": mb.market_url(m.venue, m.external_id, m.raw, m.url),
            }
        )

    # ---- distribution-ladder completion -----------------------------------
    # A range/distribution anchor's reading is P(price > spot) over the WHOLE
    # ladder, but the index's max_markets cap can drop bins (BTC lists ~159k
    # markets, cap 1000) — often the far-from-spot tail, which truncates the
    # ladder and biases the reading. Pull the full Kalshi event ladder for each
    # distribution anchor so the holdings + reading aren't truncated. Kalshi bins
    # of one event share the external_id prefix "{EVENT}-B{strike}", so a LIKE on
    # the prefix is cheap (and doesn't touch the directional score, which still
    # uses only the gated components).
    present_ids = set(market_ids)
    ladder_prefixes: dict[str, tuple[str, str]] = {}  # event prefix -> (anchor, label)
    for r in rows:
        if not r["anchor"].startswith("range:"):
            continue
        m = market_by_id.get(r["market_id"])
        if m and m.venue == "kalshi" and m.external_id and "-B" in m.external_id:
            ladder_prefixes[m.external_id.rsplit("-B", 1)[0]] = (r["anchor"], r["anchor_label"])
    if ladder_prefixes:
        extra_stmt = select(CoreMarket).where(
            CoreMarket.venue == "kalshi",
            *live_market_conditions(),
            or_(*[CoreMarket.external_id.like(f"{p}-B%") for p in ladder_prefixes]),
        )
        if ir.min_volume_24h:
            extra_stmt = extra_stmt.where(CoreMarket.last_volume_24h >= ir.min_volume_24h)
        for m in (await session.execute(extra_stmt)).scalars().all():
            if m.id in present_ids or not m.external_id or "-B" not in m.external_id:
                continue
            info = ladder_prefixes.get(m.external_id.rsplit("-B", 1)[0])
            if info is None:
                continue
            present_ids.add(m.id)
            anchor_key, anchor_label = info
            price = m.last_price
            rows.append(
                {
                    "market_id": m.id,
                    "title": m.title,
                    "prob": round(float(price) * 100.0, 1) if price is not None else None,
                    "temp": 50.0,  # a range bin is non-directional
                    "venues": [m.venue] if m.venue else [],
                    "tags": [
                        mb.driver_name("range", asset),
                        _VENUE_DISPLAY.get(m.venue or "", (m.venue or "").title()),
                    ],
                    "bucket": "range",
                    "anchor": anchor_key,
                    "anchor_label": anchor_label,
                    "contracts": 0,
                    "volume_24h": float(m.last_volume_24h) if m.last_volume_24h is not None else None,
                    "url": mb.market_url(m.venue, m.external_id, m.raw, m.url),
                }
            )

    regime = mb.regime_for(score_val)
    return {
        "index_id": index_def.index_id,
        "version": index_def.version,
        "as_of": score.as_of.isoformat(),
        "score": score_val,
        "regime": regime,
        "implied_spot": implied_spot,
        "live_contracts": live_contracts,
        "exchanges": exchanges,
        "holdings": len(drivers),
        "component_count": score.component_count,
        "drivers": drivers,
        "series": series,
        "rows": rows,
        "delta_24h_same_basket": delta_24h_same_basket,
        "basket_turnover_24h": basket_turnover_24h,
    }
