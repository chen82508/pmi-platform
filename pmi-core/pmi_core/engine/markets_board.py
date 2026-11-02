"""Pure helpers for GET /indexes/{id}/markets-board.

Kept free of SQLAlchemy so the grouping / reading math is unit-testable in
isolation. The route in `routes/indexes.py` resolves components exactly like
`/explain` and hands the per-market tuples here.

Driver grouping: the BTC bull/bear component set is a dense strike ladder
(one venue dominates), so grouping by venue is degenerate. Instead we bucket
by log-moneyness ln(strike / implied_spot) — the same coordinate the
aggregator's moneyness kernel uses (`pmi_core.engine.moneyness`) — plus a
`range` bucket for "$X to $Y" bin markets that carry no single strike.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

# Anchor-question buckets: cluster contracts by the QUESTION they ask - the way
# the War index groups markets by country - rather than by strike distance from
# spot. Patterns are tried in order, so the specific question types (intraday /
# ATH / monthly average / range) match before the generic above/below level
# (e.g. "trimmed mean above $X" lands in Monthly average, not Above).
_QUESTION_BUCKETS: list[tuple[str, str, re.Pattern[str]]] = [
    ("intraday", "Intraday direction",
     re.compile(r"next\s+\d+\s*(?:min|hour)|up or down|hourly|higher or lower", re.IGNORECASE)),
    ("ath", "All-time high",
     re.compile(r"all[-\s]?time high|\bnew high\b|\bath\b|record high", re.IGNORECASE)),
    ("monthly_avg", "Monthly average",
     re.compile(r"trimmed mean|average price|\bmonthly\b", re.IGNORECASE)),
    ("range", "Price range",
     re.compile(r"\brange\b|\d[\d,]{2,}\s*to\s*\$?\s?\d", re.IGNORECASE)),
    ("above", "Above a price level",
     re.compile(r"\babove\b|\bexceed\b|\breach\b|\bhit\b|\bover\b", re.IGNORECASE)),
    ("below", "Below a price level",
     re.compile(r"\bbelow\b|\bdip to\b|\bunder\b|\bfalls? to\b", re.IGNORECASE)),
]

# Display order for the composition strip / driver board + holdings tabs.
DRIVER_ORDER = [key for key, *_ in _QUESTION_BUCKETS] + ["other"]

DRIVER_NAMES: dict[str, str] = {key: name for key, name, _ in _QUESTION_BUCKETS} | {
    "other": "Other markets",
}

# Bull/bear boards cover different underlying assets, so the anchor labels and
# the catch-all driver name must name the RIGHT asset (BTC vs S&P 500) instead of
# always saying "BTC". Prefer the explicit map; else strip the "Bull/Bear Index"
# suffix off the def title.
_ASSET_LABELS: dict[str, str] = {
    "btc-bull-bear-index": "BTC",
    "sp500-bull-bear-index": "S&P 500",
}


def asset_label(index_id: str | None, title: str | None = None) -> str:
    if index_id and index_id in _ASSET_LABELS:
        return _ASSET_LABELS[index_id]
    if title:
        stripped = re.sub(r"\s*bull\s*/?\s*bear.*$", "", title, flags=re.IGNORECASE).strip()
        if stripped:
            return stripped
    return "the index"


def driver_name(key: str, asset: str = "BTC") -> str:
    """Driver-bucket display name; the catch-all bucket names the asset."""
    if key == "other":
        return f"Other {asset} markets"
    return DRIVER_NAMES.get(key, key)


def classify(title: str | None, implied_spot: float | None = None) -> str:
    """Return an anchor-question driver key, deterministic from the title.

    Groups a contract by the question it asks (price above/below a level, a
    range, the monthly average, an intraday move, a new all-time high) - the
    BTC analogue of the War index's by-country grouping. implied_spot is kept
    for signature stability but no longer used (grouping is spot-independent).
    """
    text = title or ""
    for key, _name, pattern in _QUESTION_BUCKETS:
        if pattern.search(text):
            return key
    return "other"


def _deslug(slug: str) -> str:
    """"what-price-will-bitcoin-hit-before-2027" -> "What Price Will Bitcoin Hit Before 2027"."""
    return " ".join(w.capitalize() for w in slug.split("-") if w)


_STRIKE_SUB = re.compile(r"\$\s?[0-9][0-9,]*(?:\.[0-9]+)?")
_TIME_SUB = re.compile(r"\s+at\s+\d{1,2}:\d\d\s*[AP]?M?\s*(?:ET|CT|UTC)", re.IGNORECASE)


_MONTHS = {}
for _i, _m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
):
    _MONTHS[_m] = _i
_DEFAULT_YEAR = 2026

_DATE_RE = re.compile(
    r"\b(by|on|in)\s+"
    r"(?:"
    r"(?P<mon1>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<yr1>\d{4}))?"
    r"|(?P<yr2>\d{4})"
    r"|(?P<mon2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s*(?P<yr3>\d{4}))?(?!\s*\d)"
    r")",
    re.IGNORECASE,
)


def _anchor_when(title: str | None) -> tuple[str, str, str] | None:
    """(res_type, date_key, date_label) or None. res_type is 'max' for a window
    ("by"/"in {date}" - touched any time through the period) vs 'close' for a
    point-in-time ("on {date}"). date_key is an ISO day used for grouping; equal
    keys across venues fold together."""
    m = _DATE_RE.search(title or "")
    if not m:
        return None
    res = "close" if m.group(1).lower() == "on" else "max"
    if m.group("mon1"):
        mon = _MONTHS[m.group("mon1").lower()[:3]]
        day = int(m.group("day"))
        yr = int(m.group("yr1")) if m.group("yr1") else _DEFAULT_YEAR
        label = f"{m.group('mon1').title()[:3]} {day}, {yr}"
        return res, f"{yr:04d}-{mon:02d}-{day:02d}", label
    if m.group("yr2"):
        yr = int(m.group("yr2"))
        return "max", f"{yr:04d}-12-31", str(yr)
    if m.group("mon2"):
        mon = _MONTHS[m.group("mon2").lower()[:3]]
        yr = int(m.group("yr3")) if m.group("yr3") else _DEFAULT_YEAR
        last = calendar.monthrange(yr, mon)[1]
        label = f"{m.group('mon2').title()} {yr}"
        return "max", f"{yr:04d}-{mon:02d}-{last:02d}", label
    return None


_RANGE_PAIR_RE = re.compile(r"\b\d[\d,]{2,}\s+to\s+\d[\d,]{2,}\b")


_WHICH_FIRST_RE = re.compile(r"\$\s?[\d,.]+\s+(?:before|vs\.?|versus)\s+\$?\s?[\d,.]+", re.IGNORECASE)


def _direction_tag(title: str | None) -> str | None:
    """up / down / range for the cross-venue anchor key, from the question's
    wording. range = a bounded "$A-$B" / "A to B" bin (non-directional). A
    "which threshold first" bet ("hit $50k before $100k") is neither: it returns
    None so anchor_of falls back to a per-market key with a faithful label rather
    than mislabelling it as a price bin or a plain "above" ladder."""
    t = title or ""
    if _WHICH_FIRST_RE.search(t):
        return None
    if re.search(r"\bbetween\b", t, re.IGNORECASE):
        return "range"
    if len(re.findall(r"\$\s?\d[\d,]*", t)) >= 2:
        return "range"
    if _RANGE_PAIR_RE.search(t):
        return "range"
    if re.search(r"\b(above|exceeds?|reach(?:es)?|over|greater|hits?|surpass(?:es)?)\b", t, re.IGNORECASE):
        return "up"
    if re.search(r"\b(below|under|beneath|dips?|falls?|drops?|less|lower)\b", t, re.IGNORECASE):
        return "down"
    return None


def anchor_of(
    venue: str | None,
    external_id: str | None,
    raw: dict | None,
    title: str | None,
    asset: str = "BTC",
) -> tuple[str, str]:
    """(anchor_key, anchor_label) — the underlying question a contract belongs to,
    the BTC analogue of a War country. Grouped venue-agnostically by (direction,
    resolution-type, date) so the *same* question folds across exchanges - e.g.
    kalshi "above $X by Dec 31, 2026", polymarket "reach $X by December 31, 2026"
    and forecastex "exceed $X in 2026" all collapse into one "BTC above by Dec 31,
    2026" ladder. Contracts whose wording does not parse fall back to the old
    per-venue series/event grouping so nothing is lost."""
    d = _direction_tag(title)
    when = _anchor_when(title)
    if d and when:
        res, dkey, _ = when
        word = {"up": "above", "down": "below", "range": "price"}[d]
        prep = "on" if res == "close" else "by"
        y, mo, dd = dkey.split("-")
        label_date = f"{calendar.month_abbr[int(mo)]} {int(dd)}, {y}"
        return f"{d}:{res}:{dkey}", f"{asset} {word} {prep} {label_date}"
    venue = venue or ""
    ext = external_id or ""
    raw = raw or {}
    if venue == "polymarket":
        events = raw.get("events") or []
        slug = (events[0].get("slug") if events and isinstance(events[0], dict) else "") or ""
        if slug:
            return "p:" + slug, _deslug(slug)
        key = "p:event"
    elif venue == "kalshi":
        key = "k:" + (ext.split("-")[0] or "kalshi")
    elif venue == "forecastex":
        key = "f:" + (ext.split("_")[0] or "forecastex")
    else:
        key = venue or "other"
    # Fallback label from the title: drop the kalshi "- Above $X" suffix, the
    # time-of-day tail, and normalise the strike to "$X" so ladder strikes merge.
    label = (title or "").split(" - ")[0]
    label = _STRIKE_SUB.sub("$X", label)
    label = _TIME_SUB.sub("", label)
    label = re.sub(r"\s+", " ", label).strip().rstrip("?")
    return key, label or (title or f"{asset} market")


def market_url(
    venue: str | None, external_id: str | None, raw: dict | None, url: str | None
) -> str | None:
    """Public page for a contract. Prefer the stored url; otherwise derive one
    from the venue slug so a card always links somewhere. The DB url is commonly
    null for polymarket even though the event slug is present in raw."""
    if url:
        return url
    venue = venue or ""
    raw = raw or {}
    if venue == "polymarket":
        events = raw.get("events") or []
        ev_slug = (events[0].get("slug") if events and isinstance(events[0], dict) else "") or ""
        slug = ev_slug or (raw.get("slug") or "")
        if slug:
            return f"https://polymarket.com/event/{slug}"
    if venue == "kalshi":
        # Only the REST poller stores a Kalshi url (from series_ticker it collects
        # off the rate-limited /events walk), so markets ingested via clob/ws/etc.
        # arrive with a null url. Derive one here from the event_ticker — the same
        # fallback the poller uses — so EVERY Kalshi market links. With a known
        # series the canonical {series}/{event} page; else the series page (a
        # single-segment /markets/{full-ticker} 404s to an empty shell).
        et = raw.get("event_ticker") or (
            external_id.rsplit("-", 1)[0] if external_id and "-" in external_id else ""
        )
        if et:
            series_full = raw.get("series_ticker")
            if series_full:
                return f"https://kalshi.com/markets/{series_full.lower()}/{et.lower()}"
            derived = et.split("-", 1)[0]
            if derived:
                return f"https://kalshi.com/markets/{derived.lower()}"
    return None


@dataclass(frozen=True)
class Member:
    """One component market as the reading math sees it."""

    market_id: int
    relevancy: float  # 0–1 weighted factor mean
    direction: float  # -1 / 0 / +1
    price: float | None  # last price 0–1, None = no snapshot yet


def signed_reading(members: list[Member]) -> float | None:
    """Relevancy-weighted mean of direction·(2p−1), mapped to 0–100.

    Same centering as the index's `signed_weighted_average_x_100` formula
    (price 0.50 → 50 = balanced book), WITHOUT the liquidity / moneyness
    kernels — those are whole-index normalizations; re-applying them per
    driver group would double-count.

    None when no member has a price (nothing to read).
    """
    priced = [m for m in members if m.price is not None]
    if not priced:
        return None
    num = sum(m.relevancy * m.direction * (2.0 * m.price - 1.0) for m in priced)
    den = sum(m.relevancy for m in priced)
    if den <= 0:
        return None
    raw = num / den  # ∈ [-1, +1]
    return round(max(0.0, min(100.0, (raw + 1.0) / 2.0 * 100.0)), 2)


_REGIMES: list[tuple[float, str]] = [
    (20.0, "Deep bear"),
    (40.0, "Bearish"),
    (60.0, "Neutral"),
    (80.0, "Bullish"),
    (float("inf"), "Euphoric"),
]


def regime_for(score: float | None) -> str:
    if score is None:
        return "No signal"
    return next(label for hi, label in _REGIMES if score < hi)


def fmt_strike(value: float) -> str:
    """$59,900 → '$59.9k'; $102,000 → '$102k' — compact tile subtitles."""
    if value >= 1_000:
        k = value / 1_000.0
        return f"${k:,.1f}k".replace(".0k", "k")
    return f"${value:,.0f}"
