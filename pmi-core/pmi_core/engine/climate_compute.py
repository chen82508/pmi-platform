"""Climate & Weather vertical compute — shared by the climate-snapshot worker
(writes ts_page_cache) and the pmi-api /climate cold-cache fallback.

The vertical is a hub of sub-boards (like /markets): this module is being built
board-by-board; the Global Warming board lands first. Two platform rules drive
the design:

* **Two-tier membership.** Kalshi climate markets live under stable series
  prefixes (``KXGTEMP`` hottest-year, ``KXHMONTH``/``KXHMONTHRANGE`` hottest-month
  + anomaly, ``KXHIGH*`` city highs) — the **scored core**
  keys on those (skill §9: stable IDs, never a title regex). The other exchanges
  (forecastex real-money; manifold/metaculus/poly) have no such series, so they're
  pulled by a **curated keyword + strict reject** filter (``_WARM_*``
  below) and enter the **display** layer only — holdings, the exchange count, the
  contract count. They do NOT join the headline fold unless they're the *same*
  question as an anchor (e.g. "hottest year on record" across venues), because a
  forecastex ">1.75 °C threshold" market is a different question from "record-hot
  year" and averaging them corrupts the reading. This is the display rule: show
  every exchange that has contracts; weighting them into the number is separate.
  The reject terms kill title collisions (warming→superconductor / Ragnarök).
* **Play-money (metaculus/manifold) is DISPLAYED, not weighted** (see the
  pmi-playmoney-display-rule memory): it rides the holdings/venue list so the UI
  is rich, but contributes 0 to a board score (``_member`` sets weight 0).
* **City Heat is kalshi-only by nature** — no other exchange runs per-city
  daily-high strike ladders (forecastex's temp markets are national monthly, not
  city-daily), so there's nothing to broaden there.

Direction is uniform across the vertical: **higher = hotter / more extreme.**
Returns plain JSON-serialisable dicts (no pmi-api imports).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.engine.market_status import live_market_conditions
from pmi_core.models import CoreMarket
from pmi_core.venues import has_real_money

CLIMATE_INDEX_ID = "climate"
# The sub-boards compute_climate produces (canonical list — the API guards
# unknown board names against this instead of paying a full recompute).
CLIMATE_BOARDS = ("global-warming", "city-heat")

REAL_MONEY = ("kalshi", "polymarket", "forecastex", "robinhood")
PLAY_MONEY = ("metaculus", "manifold")  # displayed, weight 0
# US-facing site → temperatures in °F. Anomalies are DIFFERENCES, so the °C→°F
# conversion is ×9/5 with NO +32 offset (a +1.25 °C anomaly is +2.25 °F, which is
# exactly how forecastex writes it: "1.25 C (2.25 F)").
_C_TO_F = 9.0 / 5.0
# Uniform 0–100 direction poles for every climate board (skill §5 direction cue).
POLES = {"low": "Cooler", "high": "Hotter"}

# ── Global Warming board — stable-series membership ───────────────────────────
# Kalshi: KXGTEMP-{yy}  (P year is hottest on record), KXHMONTH-{yymon} (P month
# hottest ever), KXHMONTHRANGE-{yymon}-B{mid} (monthly temp-anomaly distribution).
# Polymarket has no series ticker, so the few genuine "hottest year on record"
# markets are matched by their exact phrase (a tight, curated title — not a broad
# keyword) and confirmed to be binary Yes/No.
_KALSHI_WARMING = re.compile(r"^KX(GTEMP|HMONTH|HMONTHRANGE)-", re.IGNORECASE)
_POLY_WARMING_TITLE = "%hottest year on record%"

# Non-kalshi venue capture (display layer). Curated keyword must hit a real
# warming signal; the reject list removes the metaculus/poly junk that shares a
# word ("global temperature" but about superconductors, nuclear winter, bets…).
# NB: no bare "on record" term — it greedily hits "marathon record"/"world
# record"; the record-year phrasing is already covered by hottest/warmest year.
_WARM_KW = (
    r"hottest (year|month|summer)|warmest (year|month|august|summer)|"
    r"global warming|global temperature|average (global|u\.?s\.?) temperature|"
    r"temperature anomaly|warming reach"
)
_WARM_REJECT = (
    r"superconductor|room.?temperature|body temperature|ragnar|nuclear|detonation|"
    r"\bwar\b|catastroph|solar radiation|hurricane|world cup|\bnba\b|\bnhl\b"
)
# Same-question-as-hottest-year, across any non-kalshi venue → folds into that
# anchor. `_RANK_HOTTEST` excludes the ordinal-rank variants (polymarket lists
# "second-/third-/…/sixth-hottest year on record") — those are "is it the Nth
# hottest", a different question from "is it THE hottest", and must not pollute
# the P(record) fold.
_TITLE_RECORD_YEAR = re.compile(r"hottest year|year.*on record", re.IGNORECASE)
_RANK_HOTTEST = re.compile(r"-hottest\b", re.IGNORECASE)

# Kalshi climate tickers carry the target period: KXGTEMP-{yy}, KXHMONTH-{yy}{mon3},
# KXHMONTHRANGE-{yy}{mon3}-B{mid} (e.g. kxhmonthrange-26sep). Parse them so a board
# folds ONE year / ONE month, not a mix of different-target questions.
_MON3 = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1,
)}
_KALSHI_YEAR_RE = re.compile(r"^KX(?:GTEMP|HMONTH|HMONTHRANGE)-(\d\d)", re.IGNORECASE)
_KALSHI_YM_RE = re.compile(r"^KX(?:HMONTH|HMONTHRANGE)-(\d\d)([a-z]{3})", re.IGNORECASE)
_TITLE_YEAR_RE = re.compile(r"\b(20\d\d)\b")


def _kalshi_year(ext: str | None) -> int | None:
    """2026 from KXGTEMP-26 / KXHMONTH-26SEP / KXHMONTHRANGE-26SEP-Bxx, else None."""
    m = _KALSHI_YEAR_RE.match(ext or "")
    return 2000 + int(m.group(1)) if m else None


def _kalshi_yearmonth(ext: str | None) -> tuple[int, int] | None:
    """(2026, 9) from KXHMONTH-26SEP / KXHMONTHRANGE-26SEP-Bxx, else None."""
    m = _KALSHI_YM_RE.match(ext or "")
    if not m:
        return None
    mon = _MON3.get(m.group(2).lower())
    return (2000 + int(m.group(1)), mon) if mon else None


def _title_year(title: str | None) -> int | None:
    """The 4-digit year a title targets ("Will 2027 be the warmest year?" → 2027)."""
    m = _TITLE_YEAR_RE.search(title or "")
    return int(m.group(1)) if m else None


def _pick_month(items: list[tuple]) -> list[tuple]:
    """From [(row, member, (year,month)), …] keep only the month nearest to now —
    the current month if present, else the nearest upcoming, else the latest — so a
    monthly board reads ONE month instead of averaging Aug + Sep."""
    if not items:
        return []
    now = datetime.now(UTC)
    cur = (now.year, now.month)
    months = {ym for *_, ym in items}
    upcoming = sorted(ym for ym in months if ym >= cur)
    target = upcoming[0] if upcoming else max(months)
    return [t for t in items if t[-1] == target]

# Bucket midpoint from a ladder leg's yes_sub_title — general over °C anomaly
# ("1.04 to 1.09" → 1.065) and integer °F temps ("75 to 76" / "80° or above").
_NUM = r"(-?[0-9]+(?:\.[0-9]+)?)"
_RANGE_YS = re.compile(rf"{_NUM}\s*°?\s*(?:to|–|-)\s*{_NUM}")
_RANGE_ABOVE = re.compile(rf"{_NUM}\s*(?:°\s*)?(?:or\s*(?:above|higher|more)|\+|and\s*above)", re.IGNORECASE)
_RANGE_BELOW = re.compile(rf"(?:below|under)\s*{_NUM}", re.IGNORECASE)
_SINGLE = re.compile(_NUM)


def _bucket_midpoint(yes_sub: str | None) -> float | None:
    """Numeric value implied by a distribution-ladder leg's yes_sub_title, else
    None. Handles ranges, open-ended edges, and a bare single number."""
    if not yes_sub:
        return None
    if m := _RANGE_YS.search(yes_sub):
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    if m := _RANGE_ABOVE.search(yes_sub):
        return float(m.group(1))
    if m := _RANGE_BELOW.search(yes_sub):
        return float(m.group(1))
    if m := _SINGLE.search(yes_sub):
        return float(m.group(1))
    return None


# ── forecastex threshold ladders → E[°C] (same family as the BTC strike ladder:
# a set of "P(value > threshold)" markets is a survival function; integrate it
# for the implied expectation). forecastex titles read "…Average Global
# Temperature in {YEAR} be greater than {X} degrees Celsius above the 20th
# century average?". We parse (scope, year, threshold) and fold one ladder per
# (scope, year) into E. Cumulative "for any year … by the end of 20xx" and
# monthly ("in August 2026") variants are a different quantity → left in holdings.
_FX_SCOPE = re.compile(r"average\s+(global|u\.?s\.?)\s+temperature", re.IGNORECASE)
_FX_ANY_YEAR = re.compile(r"for\s+any\s+year", re.IGNORECASE)
_FX_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d\d", re.IGNORECASE
)
_FX_YEAR = re.compile(r"\bin\s+(20\d\d)\b", re.IGNORECASE)
# threshold: prefer the explicit Celsius figure ("1.25 C (2.25 F)" / "1.0 degrees
# Celsius"); the parenthetical F is a conversion, not a separate strike.
_FX_C = re.compile(r"greater than\s+(-?[0-9]+(?:\.[0-9]+)?)\s*(?:c\b|degrees?\s*celsius)", re.IGNORECASE)


def _fx_anomaly_leg(title: str | None) -> tuple[str, int, float] | None:
    """(scope, year, threshold_°C) for a forecastex *global annual* anomaly market,
    else None. Only global + single-year + Celsius (the headline-comparable
    quantity) is parsed here."""
    if not title:
        return None
    sc = _FX_SCOPE.search(title)
    if not sc or _FX_ANY_YEAR.search(title) or _FX_MONTH.search(title):
        return None
    scope = "global" if sc.group(1).lower().startswith("global") else "us"
    if scope != "global":
        return None  # US ladders are °F — displayed, not folded here yet
    ym = _FX_YEAR.search(title)
    cm = _FX_C.search(title)
    if not ym or not cm:
        return None
    return scope, int(ym.group(1)), float(cm.group(1))


def _pava_nonincreasing(vals: list[float]) -> list[float]:
    """Least-squares monotone-NON-INCREASING fit (pool-adjacent-violators). Thin
    last-trade prices make the raw survival curve non-monotonic; this cleans it."""
    stack: list[list[float]] = []  # [mean, weight]
    for v in vals:
        cur = [v, 1.0]
        while stack and stack[-1][0] < cur[0]:  # prev < cur violates non-increasing
            pv, pw = stack.pop()
            cur = [(pv * pw + cur[0] * cur[1]) / (pw + cur[1]), pw + cur[1]]
        stack.append(cur)
    out: list[float] = []
    for mean, weight in stack:
        out.extend([mean] * int(weight))
    return out


def _survival_expected(pairs: list[tuple[float, float]]) -> float | None:
    """E[value] implied by a survival ladder [(threshold, P(value>threshold))].
    Sort, clamp to [0,1], enforce monotonicity, then integrate the implied mass
    at bin midpoints with half-gap tails. Needs ≥3 rungs to be meaningful."""
    pts = sorted(pairs)
    if len(pts) < 3:
        return None
    ts = [t for t, _ in pts]
    ss = _pava_nonincreasing([max(0.0, min(1.0, p)) for _, p in pts])
    e = 0.0
    first_gap, last_gap = ts[1] - ts[0], ts[-1] - ts[-2]
    e += (1.0 - ss[0]) * (ts[0] - first_gap / 2)          # mass below the lowest rung
    for i in range(len(ts) - 1):
        e += (ss[i] - ss[i + 1]) * ((ts[i] + ts[i + 1]) / 2)
    e += ss[-1] * (ts[-1] + last_gap / 2)                  # mass above the highest rung
    return round(e, 2)


async def _members_where(session: AsyncSession, *conds) -> list:
    """Live member rows (standard column set) matching the given id conditions.

    We do NOT collapse rows that merely share a title: a distinct external_id is a
    distinct market with its own order book (e.g. Kalshi lists the same "hottest
    year" question under two events, KXGTEMP-26 and KXGTEMP-27, priced 0.71 vs
    0.67). Those are different contracts and each is shown."""
    stmt = select(
        CoreMarket.id,
        CoreMarket.venue,
        CoreMarket.title,
        CoreMarket.external_id,
        CoreMarket.last_price,
        CoreMarket.last_volume_24h,
        CoreMarket.last_price_at,
        CoreMarket.url,
        CoreMarket.raw.op("->>")("yes_sub_title").label("yes_sub"),
        CoreMarket.is_play_money,
    ).where(
        *live_market_conditions(),
        CoreMarket.last_price.isnot(None),
        *conds,
    )
    return (await session.execute(stmt)).all()


async def _warming_rows(session: AsyncSession) -> list:
    """Live Global-Warming member rows: kalshi scored core (stable series) UNION
    the curated-keyword display layer across every other exchange."""
    return await _members_where(
        session,
        or_(
            CoreMarket.external_id.op("~*")(r"^KX(GTEMP|HMONTH|HMONTHRANGE)-"),
            CoreMarket.title.ilike(_POLY_WARMING_TITLE),
            and_(
                CoreMarket.title.op("~*")(_WARM_KW),
                CoreMarket.title.op("!~*")(_WARM_REJECT),
            ),
        ),
    )


def _member(r) -> dict:
    """One member contract as a plain dict (holdings shape). `weight` is 0 for
    play-money (displayed, not scored)."""
    return {
        "market_id": r.id,
        "venue": r.venue,
        "title": r.title,
        "price": round(float(r.last_price) * 100.0, 1),
        "volume_24h": float(r.last_volume_24h) if r.last_volume_24h is not None else None,
        "url": r.url,
        "play_money": bool(r.is_play_money),
        "weight": 0.0 if r.is_play_money else 1.0,
    }


def _sum_vol(pairs: list[tuple]) -> float | None:
    """Summed REAL-money 24h volume across an anchor's contracts (None when no
    real-money contract carries one) — every drill-in PmiCard shows this as
    dollars. Play-money "volume" is site credits (Manifold mana) and must not be
    added to a dollar figure: the "Hottest year" card read $39K of which $27K was
    a Manifold market (2026-10-12). Play-money legs stay listed with their own
    figure, flagged ``play_money``."""
    vols = [
        m["volume_24h"]
        for _, m in pairs
        if m["volume_24h"] is not None and not m.get("play_money")
    ]
    return sum(vols) if vols else None


def _kalshi_series(external_id: str | None) -> str | None:
    if not external_id:
        return None
    m = _KALSHI_WARMING.match(external_id)
    return m.group(1).upper() if m else None


def _question(label: str, reading: float | None, unit: str, poles: dict, member_pairs: list) -> dict:
    """One anchor's L2 detail: its reading + the contract subset backing it. The
    trend is the question cache key's own accumulated history (scalar_points), so
    no per-hour re-fold is needed here."""
    contracts = [m for _, m in member_pairs]
    return {
        "label": label,
        "reading": reading,
        "unit": unit,
        "poles": poles,
        "contracts": sorted(contracts, key=lambda m: -(m["volume_24h"] or 0.0)),
        "exchanges": sorted({m["venue"] for m in contracts}),
        "n_contracts": len(contracts),
    }


def _reading_layer(questions: dict) -> tuple[int, list[str]]:
    """(contract count, exchanges) behind the board's question cards — the layer
    the page header and API summary report. `members` also holds display-only
    matches that fold into no card (other years/months, a city's other days,
    keyword hits), so counting it put 160 in the header over cards summing to 50."""
    contracts = {c["market_id"]: c for q in questions.values() for c in q["contracts"]}
    return len(contracts), sorted({c["venue"] for c in contracts.values()})


def _warming_board(rows: list) -> dict:
    """Fold the Global-Warming members into a headline PMI + anchor questions.

    Headline = market-implied P(the current record-year is the hottest on record)
    — real-money, volume-weighted — on the 0–100 "Cooler→Hotter" axis. Anchors:
    hottest-year (per year), hottest-month (per month), and the monthly anomaly
    distribution's E[°C]."""
    members = [_member(r) for r in rows]

    # Split by question type off the stable series; non-kalshi "hottest year on
    # record" markets (poly real-money, manifold play-money) fold into the same
    # anchor. Everything else stays display-only (in `members` → holdings/count).
    target_year = datetime.now(UTC).year
    hottest_year: list[tuple] = []   # (row, member) — kalshi KXGTEMP, current year only
    hottest_month_all: list[tuple] = []  # (row, member, (yy,mm)) — narrowed to one month below
    anomaly_all: list[tuple] = []        # (row, member, (yy,mm))
    record_year: list[tuple] = []    # same question, other exchanges
    # forecastex global-annual anomaly survival ladders, grouped by year:
    # {year: [(threshold_°C, row, member)]}
    fx_annual: dict[int, list[tuple]] = {}
    for r, m in zip(rows, members, strict=False):
        s = _kalshi_series(r.external_id)
        if s == "GTEMP":
            # KXGTEMP-{yy}: only the current record-year question — folding
            # "will 2027/2030/2035 be the warmest year?" mixes different questions.
            if _kalshi_year(r.external_id) == target_year:
                hottest_year.append((r, m))
        elif s == "HMONTH":
            if (ym := _kalshi_yearmonth(r.external_id)) is not None:
                hottest_month_all.append((r, m, ym))
        elif s == "HMONTHRANGE":
            if (ym := _kalshi_yearmonth(r.external_id)) is not None:
                anomaly_all.append((r, m, ym))
        elif s is None and _TITLE_RECORD_YEAR.search(r.title or "") and not _RANK_HOTTEST.search(r.title or ""):
            # Other-venue "hottest year": keep only the current year (drop
            # "Will 2027 be the warmest year?"); untitled-year phrasing = current.
            ty = _title_year(r.title)
            if ty is None or ty == target_year:
                record_year.append((r, m))
        if r.venue == "forecastex" and (leg := _fx_anomaly_leg(r.title)):
            fx_annual.setdefault(leg[1], []).append((leg[2], r, m))

    # HMONTH / HMONTHRANGE are per-month questions; fold ONE month (nearest to now)
    # so the reading isn't an Aug+Sep average.
    hottest_month = [(r, m) for r, m, _ in _pick_month(hottest_month_all)]
    anomaly = [(r, m) for r, m, _ in _pick_month(anomaly_all)]

    def _vw_yes(rows_members: list[tuple]) -> float | None:
        """Volume-weighted mean P(YES)*100 across real-money legs (play-money w=0)."""
        num = den = 0.0
        for _r, m in rows_members:
            if m["play_money"]:
                continue
            w = (m["volume_24h"] or 0.0) + 1.0  # +1 so a 0-vol real leg still counts
            num += m["price"] * w
            den += w
        return round(num / den, 1) if den else None

    # Headline: P(a record-hot year) — blend Kalshi KXGTEMP + Polymarket "hottest
    # year on record" (real-money). Both are P(YES) on the same question family.
    headline = _vw_yes(hottest_year + record_year)

    # E[monthly anomaly] from the KXHMONTHRANGE distribution (Σ p·midpoint / Σ p),
    # in °F (buckets are °C anomalies → ×9/5).
    e_anom = None
    if anomaly:
        pnum = psum = 0.0
        for r, m in anomaly:
            mid = _bucket_midpoint(r.yes_sub)
            if mid is None:
                continue
            p = m["price"] / 100.0
            pnum += p * mid
            psum += p
        e_anom = round(pnum / psum * _C_TO_F, 2) if psum else None

    # E[global annual anomaly] per year from the forecastex survival ladders,
    # converted °C→°F. {year: (E°F, [(row, member)])}. The flagship (headline_stat)
    # is the nearest year present (typically the current 2026).
    fx_expected: dict[int, tuple] = {}
    for year, legs in fx_annual.items():
        e = _survival_expected([(thr, m["price"] / 100.0) for thr, _, m in legs])
        if e is not None:
            fx_expected[year] = (round(e * _C_TO_F, 2), [(r, m) for _, r, m in legs])
    flagship_year = min(fx_expected) if fx_expected else None

    anchors = []
    if hottest_year or record_year:
        anchors.append({
            "key": "hottest-year", "label": "Hottest year on record",
            "reading": _vw_yes(hottest_year + record_year),
            "n_contracts": len(hottest_year) + len(record_year),
            "venues": sorted({m["venue"] for _, m in hottest_year + record_year}),
            "volume_24h": _sum_vol(hottest_year + record_year),
        })
    if hottest_month:
        anchors.append({
            "key": "hottest-month", "label": "Hottest month ever",
            "reading": _vw_yes(hottest_month),
            "n_contracts": len(hottest_month),
            "venues": sorted({m["venue"] for _, m in hottest_month}),
            "volume_24h": _sum_vol(hottest_month),
        })
    # forecastex global annual anomaly, one anchor per year (°F, real-money).
    for year in sorted(fx_expected):
        e, pairs = fx_expected[year]
        anchors.append({
            "key": f"anomaly-global-{year}", "label": f"Global temperature anomaly · {year} (°F)",
            "reading": None, "e_anomaly": e,
            "n_contracts": len(pairs),
            "venues": sorted({m["venue"] for _, m in pairs}),
            "volume_24h": _sum_vol(pairs),
        })
    if anomaly:
        anchors.append({
            "key": "temp-anomaly", "label": "Monthly temperature anomaly (°F)",
            "reading": None, "e_anomaly": e_anom,
            "n_contracts": len(anomaly),
            "venues": sorted({m["venue"] for _, m in anomaly}),
            "volume_24h": _sum_vol(anomaly),
        })

    questions = {}
    if hottest_year or record_year:
        questions["hottest-year"] = _question(
            "Hottest year on record", _vw_yes(hottest_year + record_year), "PMI", POLES, hottest_year + record_year)
    if hottest_month:
        questions["hottest-month"] = _question(
            "Hottest month ever", _vw_yes(hottest_month), "PMI", POLES, hottest_month)
    for year in sorted(fx_expected):
        e, pairs = fx_expected[year]
        questions[f"anomaly-global-{year}"] = _question(
            f"Global temperature anomaly · {year}", e, "°F", POLES, pairs)
    if anomaly:
        questions["temp-anomaly"] = _question(
            "Monthly temperature anomaly", e_anom, "°F", POLES, anomaly)

    # Unified play-money rule: drop question cards / anchors with no real-money
    # contract (all play-money). Play-money is display-only (weight 0); this keeps
    # it from creating a phantom question. Same rule the markets board + elections
    # apply. See pmi_core.venues.has_real_money.
    questions = {
        k: q for k, q in questions.items() if has_real_money(c["venue"] for c in q["contracts"])
    }
    anchors = [a for a in anchors if has_real_money(a.get("venues") or [])]
    n_reading, reading_venues = _reading_layer(questions)

    return {
        "board": "global-warming",
        "title": "Global Warming",
        "score": headline,
        "poles": POLES,
        "questions": questions,
        # Flagship: the market-implied global annual anomaly (forecastex ladder),
        # falling back to the kalshi monthly anomaly when no annual ladder is live.
        "headline_stat": (
            {"value": f"+{fx_expected[flagship_year][0]:.1f}°F",
             "label": f"Implied {flagship_year} Global Anomaly"}
            if flagship_year is not None
            else {"value": f"+{e_anom:.1f}°F", "label": "Implied Monthly Anomaly"}
            if e_anom is not None else None
        ),
        "component_count": n_reading,
        "exchanges": reading_venues,
        # Display layer (every matched contract, incl. ones in no card) — kept for
        # holdings; not what the header counts.
        "tracked_count": len(members),
        "tracked_exchanges": sorted({m["venue"] for m in members}),
        "anchors": anchors,
        "holdings": sorted(members, key=lambda m: -(m["volume_24h"] or 0.0)),
        "as_of": datetime.now(UTC).isoformat(),
    }


# City Heat direction poles. (The Hurricane Season board was removed 2026-09-07 —
# its "Cat 3+" intensity anchor read off Cat-1 Pacific storms and mixed basins.)
POLES_HEAT = {"low": "Cooler", "high": "Hotter"}

# ── City Heat board ──────────────────────────────────────────────────────────
# Kalshi KXHIGH{city} daily high-temperature ladders (a strike ladder per
# city-day). Each event → E[daily high °F] via bucket midpoints; the board is a
# browser of cities (no single 0–100 — absolute temps aren't comparable to a PMI).
_CITY_NAMES = {
    "NY": "New York", "LAX": "Los Angeles", "LA": "Los Angeles", "CHI": "Chicago",
    "MIA": "Miami", "ATL": "Atlanta", "BOS": "Boston", "DAL": "Dallas", "PHIL": "Philadelphia",
    "AUS": "Austin", "DEN": "Denver", "SEA": "Seattle", "HOU": "Houston", "PHX": "Phoenix",
    "DC": "Washington DC", "SF": "San Francisco", "MIN": "Minneapolis", "DET": "Detroit",
    "LV": "Las Vegas", "SAC": "Sacramento", "TPA": "Tampa", "ORL": "Orlando",
    "NASH": "Nashville", "POR": "Portland", "SLC": "Salt Lake City", "KC": "Kansas City",
}


def _city_code(series: str) -> str:
    return re.sub(r"^KXHIGHT?", "", series, flags=re.IGNORECASE).upper()


# Non-kalshi venues (e.g. IBKR "New York City Daily Temperature High — Above 65",
# "Highest Temperature in Austin — 100° to 101°") join the display layer by a
# curated keyword — the SAME cross-venue pattern the Global Warming board uses
# (_WARM_KW). They show as holdings / exchange badge / contract count; the per-city
# E[°F] reading stays on the kalshi KXHIGH ladders (a threshold "— Above N" market
# carries no bucket to fold, so it lands in `members` only, never `by_city`).
# The reject keeps global / body / sea "temperature" out — a global anomaly market
# belongs to Global Warming, not a city.
_CITYHEAT_KW = r"(daily|hourly) temperature|highest temperature in"
_CITYHEAT_REJECT = r"global|body|sea|ocean|water|\broom"


async def _cityheat_rows(session: AsyncSession) -> list:
    # kalshi KXHIGH{city} / KXHIGHT{city} daily-high TEMPERATURE ladders (scored
    # core) UNION the curated-keyword display layer across every other exchange.
    # `^KXHIGH` also matches KXHIGHINFLATION (a CPI market), which would render as a
    # fake city "INFLATION"; POSIX `~*` has no lookahead, so exclude non-temperature
    # series with a second condition (extend if other KXHIGH* non-temp series appear).
    return await _members_where(
        session,
        or_(
            and_(
                CoreMarket.external_id.op("~*")(r"^KXHIGH"),
                CoreMarket.external_id.op("!~*")(r"^KXHIGHINFLATION"),
            ),
            and_(
                CoreMarket.title.op("~*")(_CITYHEAT_KW),
                CoreMarket.title.op("!~*")(_CITYHEAT_REJECT),
            ),
        ),
    )


def _cityheat_board(rows: list) -> dict:
    members = [_member(r) for r in rows]
    events: dict[str, list] = {}  # event ticker (city-day) → legs
    for r, m in zip(rows, members, strict=False):
        ext = r.external_id or ""
        events.setdefault(ext.rsplit("-", 1)[0], []).append((r, m))

    # One row per CITY (a city has several live day-events; keep its hottest
    # upcoming day so the list reads as a clean city ranking, not city×day dupes).
    by_city: dict[str, dict] = {}
    for event, legs in events.items():
        pnum = psum = 0.0
        for r, m in legs:
            mid = _bucket_midpoint(r.yes_sub)
            if mid is None:
                continue
            p = m["price"] / 100.0
            pnum += p * mid
            psum += p
        if psum <= 0:
            continue
        city = _CITY_NAMES.get(_city_code(event.split("-")[0]), _city_code(event.split("-")[0]))
        reading = round(pnum / psum, 0)
        cur = by_city.get(city)
        if cur is None or reading > cur["reading"]:
            by_city[city] = {"key": event.lower(), "label": city, "reading": reading,
                             "n_contracts": len(legs), "venues": sorted({m["venue"] for _, m in legs}),
                             "volume_24h": _sum_vol(legs), "_legs": legs}
    ranked = sorted(by_city.values(), key=lambda a: -(a["reading"] or 0))
    # Unified play-money rule: drop a city with no real-money contract (all play-
    # money). Play-money is display-only; same rule as the other verticals.
    ranked = [c for c in ranked if has_real_money(c["venues"])]
    hottest = ranked[0] if ranked else None
    questions = {c["key"]: _question(f"{c['label']} daily high", c["reading"], "°F", POLES_HEAT, c["_legs"])
                 for c in ranked}
    anchors = [{k: v for k, v in c.items() if k != "_legs"} for c in ranked]
    n_reading, reading_venues = _reading_layer(questions)

    return {
        "board": "city-heat", "title": "City Heat", "score": None, "poles": POLES_HEAT,
        "questions": questions,
        "headline_stat": ({"value": f"{int(hottest['reading'])}°F", "label": f"Hottest: {hottest['label']}"}
                          if hottest else None),
        "component_count": n_reading, "exchanges": reading_venues,
        "tracked_count": len(members), "tracked_exchanges": sorted({m["venue"] for m in members}),
        "anchors": anchors, "holdings": sorted(members, key=lambda m: -(m["volume_24h"] or 0.0)),
        "as_of": datetime.now(UTC).isoformat(),
    }


async def compute_climate(session: AsyncSession) -> tuple[dict, dict[str, dict]]:
    """(hub_payload, {board: detail_payload}) — the whole Climate & Weather vertical
    pre-computed. `hub` carries the headline (Global Warming) score + a card per
    board; each board detail feeds the hub's in-place board switcher."""
    warming = _warming_board(await _warming_rows(session))
    cityheat = _cityheat_board(await _cityheat_rows(session))
    boards = {"global-warming": warming, "city-heat": cityheat}

    hub = {
        "generated_at": datetime.now(UTC).isoformat(),
        "score": warming["score"],  # headline = Global Warming
        "poles": POLES,
        "boards": [
            {
                "board": b["board"], "title": b["title"], "score": b["score"],
                "headline_stat": b.get("headline_stat"),
                "component_count": b["component_count"], "exchanges": b["exchanges"],
            }
            for b in boards.values()
        ],
    }
    return hub, boards
