"""Per-anchor Bull/Bear trend — pre-computed into ts_page_cache so the
markets/sp500 By-Question detail page reads it instead of scanning
ts_price_snapshots per request (the last live-compute residual, 2026-08-20).

A faithful Python port of the pmi-web adapters `loadAnchorTrend` (directional
anchor: signed-reading mean) and `loadAnchorDistributionTrend` (range anchor:
P(price > spot) share). The invariant the JS versions guard — the trend's LAST
point equals the headline tile — is preserved by appending a synthetic `now`
point seeded with each member's current board reading (`temp`), exactly as the JS
does. Operates on the board's already-computed `rows` (READ-ONLY), so it never
perturbs the board score/payload.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.models import TsPriceSnapshot

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Adaptive trend resolution: hourly only across the recent tail, daily further
# back. A 90-day window at pure hourly is ~2160 points/market × hundreds of BTC
# members (~1.5M tuples) — too heavy for the 10-min board-snapshot cadence. This
# keeps the 7/30D chips fine and the 90D chip cheap (≈573 points/market at 90d).
_HOURLY_TAIL_DAYS = 21


def _fmt_day(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _fmt_day_hour(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    h = d.hour % 12 or 12
    return f"{_MONTHS[d.month - 1]} {d.day}, {h} {'AM' if d.hour < 12 else 'PM'}"


def _range_midpoint(title: str) -> float | None:
    """Port of anchor-score.ts rangeMidpoint — $-amount midpoint of a range bin."""
    nums: list[float] = []
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d+)?)\s*([km])?", title, re.I):
        n = float(m.group(1).replace(",", ""))
        suf = (m.group(2) or "").lower()
        if suf == "k":
            n *= 1_000
        elif suf == "m":
            n *= 1_000_000
        nums.append(n)
    if nums:
        return (nums[0] + nums[1]) / 2 if len(nums) >= 2 else nums[0]
    return None


def _dir_of(temp: float | None, prob: float | None) -> int:
    """Port of anchor-score.ts dirOf — recover bull/bear direction from the
    signed reading vs the raw price. 0 = non-directional (range) contract."""
    if temp is None or prob is None:
        return 0
    if abs(temp - prob) < 0.6:
        return 1
    if abs(temp - (100 - prob)) < 0.6:
        return -1
    return 0


async def _member_hourly(
    session: AsyncSession, market_ids: list[int], days: int, now: datetime
) -> dict[int, list[tuple[str, float]]]:
    """{market_id: [(hour_iso, price*100)]} — hourly last price, same query as the
    batch trend endpoint (DISTINCT ON (market_id, hour) keeping the hour's last)."""
    if not market_ids:
        return {}
    # Bucket hourly within the recent tail, daily beyond it — so a 90d window
    # returns ~573 points/market instead of ~2160 (see _HOURLY_TAIL_DAYS). The
    # downstream trend builders key off whatever timestamps appear here, so a
    # mixed-resolution axis just means fewer points, not a wrong shape.
    hourly_from = now - timedelta(days=_HOURLY_TAIL_DAYS)
    bucket = case(
        (
            TsPriceSnapshot.snapshot_at >= hourly_from,
            func.date_trunc("hour", TsPriceSnapshot.snapshot_at),
        ),
        else_=func.date_trunc("day", TsPriceSnapshot.snapshot_at),
    )
    rows = (
        await session.execute(
            select(TsPriceSnapshot.market_id, bucket.label("t"), TsPriceSnapshot.last_price)
            .where(
                TsPriceSnapshot.market_id.in_(market_ids),
                TsPriceSnapshot.snapshot_at >= now - timedelta(days=days),
                TsPriceSnapshot.snapshot_at <= now,
                TsPriceSnapshot.last_price.isnot(None),
            )
            .distinct(TsPriceSnapshot.market_id, bucket)
            .order_by(TsPriceSnapshot.market_id, bucket, TsPriceSnapshot.snapshot_at.desc())
        )
    ).all()
    out: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for mid, t, p in rows:
        out[mid].append((t.isoformat(), round(float(p) * 100.0, 1)))
    return out


def _axis(hours: list[str], data: list[float | None]) -> dict:
    return {
        "data": data,
        "xLabels": [
            _fmt_day(hours[0]),
            _fmt_day(hours[(len(hours) - 1) // 2]),
            _fmt_day(hours[-1]),
        ],
        "labels": [_fmt_day_hour(h) for h in hours],
    }


def _directional_trend(members: list[tuple[int, int, float]], hist, now_iso: str) -> dict:
    """members: [(market_id, direction, now_temp)]. Signed-reading mean per hour,
    forward-filled, with the synthetic now point so the last value == the tile."""
    series: list[tuple[bool, list[tuple[str, float]]]] = []
    for mid, direction, now_temp in members:
        pts = sorted(hist.get(mid, []), key=lambda x: x[0])
        flip = direction < 0
        pts = list(pts)
        if now_temp is not None:  # pre-flipped so the shared flip below recovers temp
            pts.append((now_iso, (100.0 - now_temp) if flip else now_temp))
        series.append((flip, pts))
    hours = sorted({iso for _flip, pts in series for iso, _v in pts})
    if not hours:
        return {"data": []}
    acc: list[list[float]] = [[] for _ in hours]
    for flip, pts in series:
        j, last = -1, None
        for i, h in enumerate(hours):
            while j + 1 < len(pts) and pts[j + 1][0] <= h:
                j += 1
                last = pts[j][1]
            if last is not None:
                acc[i].append((100.0 - last) if flip else last)
    data = [round(sum(v) / len(v), 1) if v else None for v in acc]
    return _axis(hours, data)


def _distribution_trend(bins: list[tuple[float, list[tuple[str, float]]]], spot: float) -> dict:
    """bins: [(midpoint, [(hour_iso, price*100)])]. P(price>spot) share per hour."""
    series = [(mid, sorted(pts, key=lambda x: x[0])) for mid, pts in bins]
    hours = sorted({iso for _mid, pts in series for iso, _v in pts})
    if not hours:
        return {"data": []}
    prob_sum = [0.0] * len(hours)
    above_sum = [0.0] * len(hours)
    for midpoint, pts in series:
        j, last = -1, None
        for i, h in enumerate(hours):
            while j + 1 < len(pts) and pts[j + 1][0] <= h:
                j += 1
                last = pts[j][1]
            if last is not None:
                p = last / 100.0
                prob_sum[i] += p
                if midpoint > spot:
                    above_sum[i] += p
    data: list[float | None] = []
    for i in range(len(hours)):
        if prob_sum[i] <= 0:
            data.append(None)
        else:
            data.append(round(max(0.0, min(100.0, 100.0 * above_sum[i] / max(prob_sum[i], 1.0))), 1))
    if all(v is None for v in data):
        return {"data": []}
    return _axis(hours, data)


async def compute_anchor_trends(
    session: AsyncSession, rows: list[dict], spot: float | None, *, days: int = 30
) -> dict[str, dict]:
    """{anchor_key: TimeChartModel} for every anchor in `rows`. Empty trends are
    dropped so a cache miss (→ live fallback / no-data) is distinguishable."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    by_anchor: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("anchor"):
            by_anchor[r["anchor"]].append(r)

    all_ids = [r["market_id"] for r in rows]
    hist = await _member_hourly(session, all_ids, days, now)

    out: dict[str, dict] = {}
    for anchor, arows in by_anchor.items():
        if anchor.startswith("range:"):
            if spot is None:
                continue
            bins = [
                (mid, hist.get(r["market_id"], []))
                for r in arows
                if (mid := _range_midpoint(r["title"] or "")) is not None
            ]
            trend = _distribution_trend(bins, spot)
        else:
            members = [
                (r["market_id"], _dir_of(r.get("temp"), r.get("prob")), r["temp"])
                for r in arows
                if _dir_of(r.get("temp"), r.get("prob")) != 0 and r.get("temp") is not None
            ]
            if not members:
                continue
            trend = _directional_trend(members, hist, now_iso)
        if trend.get("data"):
            out[anchor] = trend
    return out
