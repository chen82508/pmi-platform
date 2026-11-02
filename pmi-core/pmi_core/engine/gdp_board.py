"""Pure helpers for the US GDP Expectation Index board.

Kept free of SQLAlchemy so the grouping / reading math is unit-testable in
isolation — the route in `routes/indexes.py` resolves the component markets and
hands the per-market tuples to :func:`build_board`.

What the index measures
-----------------------
NOT a GDP forecast — a market-implied *economic-expectation sentiment* on a
0–100 scale, the BTC-Bull/Bear analogue for the macro economy: 50 = neutral
(the market expects growth ≈ potential), > 50 = optimism / expansion,
< 50 = pessimism / recession fear.

Two-layer construction (validated on real data)
-----------------------------------------------
1. Per (anchor, venue) → the market-implied **E[GDP growth %]**:
     - bucket venues (kalshi / polymarket / gemini "X% to Y%")  → a direct PMF,
       E = Σ midpoint·P / ΣP;
     - threshold ladders (forecastex "exceed X%", kalshi quarterly
       "increase by more than X%")                               → a survival
       function P(GDP > x), fit monotone (see `_isotonic_nonincreasing`), E via
       the midpoint rule with a half-step tail on each end.
2. Fold venues per anchor by liquidity (24h volume, floored at
   `_MIN_FOLD_WEIGHT`) weight, then map the folded E to sentiment against a
   neutral baseline:
       sentiment = 100 / (1 + exp(-k·(E - b)))          b = 2.0, k = 0.847
   so E = b → 50 exactly, 3% → 70, 1% → 30 (symmetric about potential growth).

Anchors are keyed venue-agnostically by (horizon, period) — e.g. every venue's
"US GDP growth in 2026" folds into one `annual:2026` anchor — the same
by-question folding the War index does by country and the BTC board does by
strike question. A perpetual index: every year/quarter that has markets becomes
an anchor; resolved periods roll off, new ones appear.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pmi_core.venues import PLAY_MONEY_VENUES

# Venues excluded from the index. gemini quotes a GDP-growth "distribution"
# whose bucket prices sum well above 1 and carries no volume — degenerate noise
# (liquidity weighting already made it negligible, but keep it out entirely).
EXCLUDE_VENUES: frozenset[str] = frozenset({"gemini"})

# ── Title prefilter ────────────────────────────────────────────────────────
# The coarse title shape the board looks for, held as data so the SQL callers
# (the /gdp route, the snapshot job) and the eval harness share one definition.
# It used to be a hand-copied ILIKE chain in each place, which is a drift risk
# the moment one of them learns a new exclusion: the harness grading this
# board's membership would then be grading a filter the board no longer runs.
#
# Coarse on purpose. `is_us` and `anchor_of` do the real work; this only has to
# be cheap enough to run over ~600 k open markets in SQL.
TITLE_REQUIRE_ALL: tuple[str, ...] = ("gdp",)
TITLE_REQUIRE_ANY: tuple[str, ...] = ("growth", "real gdp")
TITLE_REJECT_ANY: tuple[str, ...] = ("nominal", "deficit", "debt")


def title_prefilter(title: str | None) -> bool:
    """Python mirror of the SQL title prefilter, for callers holding a row already."""
    t = (title or "").lower()
    if not all(term in t for term in TITLE_REQUIRE_ALL):
        return False
    if TITLE_REQUIRE_ANY and not any(term in t for term in TITLE_REQUIRE_ANY):
        return False
    return not any(term in t for term in TITLE_REJECT_ANY)

# ── Sentiment mapping parameters ───────────────────────────────────────────
NEUTRAL_BASELINE = 2.0   # b: potential US real GDP growth ≈ 2% → sentiment 50
SENSITIVITY = 0.847      # k: E=3% → 70, E=1% → 30 (symmetric about b)
_VALUE_LIMIT = 20.0      # sanity: a parsed growth rate must be within ±20pp
_MIN_SURVIVAL_POINTS = 3  # a threshold ladder needs ≥3 strikes to read E
_MIN_PMF_POINTS = 2       # one bucket has no complement — see `_e_growth`
# How far behind an anchor's freshest quote a strike may lag and still count as
# part of the same distribution. Wide enough to tolerate a slow poll cycle,
# narrow enough to exclude a quote nobody has touched in days.
_MAX_QUOTE_SKEW = timedelta(hours=24)
# Least 24h volume a real-money venue is credited with in the cross-venue fold.
# Below this the volume says nothing about which venue to trust: Q4 2026 had one
# 10-contract ForecastEx print at 1¢ and a Kalshi ladder with no fills that day,
# and a floor of 1 handed ForecastEx 10/11 of the anchor on that print alone.
# Same $1,000 line the movers feed uses for "thin".
_MIN_FOLD_WEIGHT = 1_000.0

# ── US detection ───────────────────────────────────────────────────────────
# Any other-country / region mention disqualifies (this index is US-only).
#
# Held as STEMS matched at a word start with an open suffix. The previous form
# wrapped each stem in `\b...\b`, and a trailing boundary defeats the point of a
# stem: `\bgerman\b` cannot match "Germany", `\bital\b` cannot match "Italy",
# `\bchina\b` cannot match "Chinese". Since `\breal gdp\b` is itself a positive
# US signal (below), every such title walked the whole chain and came out
# labelled "US GDP growth · Q<n> <year>". Measured on the live corpus at the
# time of the fix: 28 of the 366 titles this filter passed were foreign.
_OTHER_STEMS: tuple[str, ...] = (
    "australia", "brazil", "canad", "chin", "euro", "france", "french",
    "german", "hong kong", "indonesia", "ital", "japan", "korea", "mexic",
    "russia", "singapore", "south africa", "spain", "spanish", "turk",
    "vietnam", "world", "global",
)
# Terms whose stem would swallow an unrelated word, so they keep both
# boundaries: `uk` would eat "Ukraine", `india` would eat "Indiana".
_OTHER_EXACT: tuple[str, ...] = ("uk", "united kingdom", "india", "indian")

_OTHER = re.compile(
    r"\b(?:" + "|".join(_OTHER_STEMS) + r")\w*"
    r"|\b(?:" + "|".join(_OTHER_EXACT) + r")\b",
    re.IGNORECASE,
)
# Free-text markets that merely mention GDP but aren't a clean growth-rate
# contract (manifold presidency × GDP combos, GDP-level ratios, stock bets).
_JUNK = re.compile(
    r"presiden|stock|prior year|greater than in|reaches|3-3-3|nate silver|"
    r"which of these|130%|50% greater",
    re.IGNORECASE,
)
_US = re.compile(r"\b(us|u\.s\.|united states)\b", re.IGNORECASE)
_KALSHI_US = re.compile(r"gdp growth in 20\d\d", re.IGNORECASE)  # country-less = US
_REAL_GDP = re.compile(r"\breal gdp\b", re.IGNORECASE)

# "Will <Proper>'s ... GDP ..." — a possessive proper-noun subject that isn't the
# US names the economy being measured, so the market is somebody else's.
#
# `_OTHER` above can only reject geographies someone thought to enumerate, which
# makes it fail OPEN: anything unlisted defaults to US. This rule fails closed on
# the one title shape that carries the subject explicitly, and it is what stops
# the next instance rather than the last one. forecastex alone publishes 549
# US-STATE contracts ("Will Texas's quarterly GDP growth exceed 0% in Q1 2027?")
# that are out of scope for a national index and are excluded today only because
# they happen not to say "real GDP" — the exact phrase that same venue uses for
# its German series. Costs nothing to run: zero of the titles currently on the
# board have a possessive subject.
_POSSESSIVE_SUBJECT = re.compile(r"\b(?!US\b|U\.S\.|America)([A-Z][a-zA-Z]+)(?:'s|s')\s")


def is_us(title: str | None) -> bool:
    """True iff the title is a clean US NATIONAL GDP growth-rate market."""
    t = title or ""
    if _JUNK.search(t) or _OTHER.search(t) or _POSSESSIVE_SUBJECT.search(t):
        return False
    return bool(_US.search(t) or _KALSHI_US.match(t.strip()) or _REAL_GDP.search(t))


# ── Anchor (horizon, period) ───────────────────────────────────────────────
_QUARTER = re.compile(r"q([1-4])\s*20(\d\d)", re.IGNORECASE)
_YEAR = re.compile(r"\b20(\d\d)\b")
# Any quarter mention, year-attached or not — used only to detect ambiguity.
_ANY_QUARTER = re.compile(r"\bq([1-4])\b", re.IGNORECASE)


def anchor_of(title: str | None) -> tuple[str, str, str] | None:
    """(horizon, period, label). `horizon` is 'quarter' or 'annual'; `period` is
    the venue-agnostic fold key ('2026Q2' / '2026'). None if no period parses.

    A title naming several quarters ("negative GDP growth during Q1, Q2, or Q3
    2026") belongs to no single one — only the year-attached quarter matches
    `_QUARTER`, so it would silently file under Q3 and read as that quarter's
    expectation. Refused instead."""
    t = title or ""
    m = _QUARTER.search(t)
    if m:
        if len(set(_ANY_QUARTER.findall(t))) > 1:
            return None
        per = f"20{m.group(2)}Q{m.group(1)}"
        return "quarter", per, f"US GDP growth · Q{m.group(1)} 20{m.group(2)}"
    y = _YEAR.search(t)
    if y:
        return "annual", f"20{y.group(1)}", f"US GDP growth · 20{y.group(1)}"
    return None


# ── Per-market reading (threshold ladder vs bucket) ────────────────────────
# A survival strike, P(growth > x). Kalshi's quarterly series says "increase by
# more than x% … - Above x%"; read as a bucket it was placed at x+0.5 and its
# cumulative prices averaged as if they were a PMF (Q3 2026 read 2.0% off a
# ladder that crosses 50% near 3%). Kalshi's annual "x% or Above" is the top
# bucket of a "x% to y%" set and correctly stays a bucket.
_THRESHOLD = re.compile(r"(?:exceed|increase by more than)\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
# Only numbers attached to a "%" are growth rates — this ignores a year in the
# title (e.g. "grow by less than 3.5% ... in 2026" reads 3.5, not 2026).
_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_LOW = re.compile(r"below|less than|or below|negative|≤|<=", re.IGNORECASE)
_HIGH = re.compile(r"greater|at least|above|or higher|\+|≥|>|come in at or", re.IGNORECASE)
_NEGATIVE = re.compile(r"negative|contract(s|ion|ed)?\b|shrink", re.IGNORECASE)


def parse_reading(title: str | None) -> tuple[str, float] | None:
    """('thr', x) for a survival threshold ("exceed x%" / "increase by more than
    x%"), or ('bkt', midpoint) for a mutually-exclusive bucket. None when no
    in-range growth value parses (the sanity gate that drops years/ratios
    mis-read as growth)."""
    t = title or ""
    m = _THRESHOLD.search(t)
    if m:
        x = float(m.group(1))
        return ("thr", x) if -_VALUE_LIMIT <= x <= _VALUE_LIMIT else None

    frag = t.split(" - ", 1)[1] if " - " in t else (t.split(" be ", 1)[-1] if " be " in t else t)
    nums = [float(n) for n in _NUM.findall(frag)]
    if len(nums) >= 2:
        x = (nums[0] + nums[1]) / 2.0
    elif len(nums) == 1:
        x = nums[0] - 0.5 if _LOW.search(frag) else (nums[0] + 0.5 if _HIGH.search(frag) else nums[0])
    elif _NEGATIVE.search(frag):
        x = -0.5  # sub-zero bucket, the one reading that needs no number
    else:
        # No percentage and no direction word — nothing to read. This used to
        # fall through to -0.5, so every unparseable GDP title asserted a
        # recession bucket.
        return None
    return ("bkt", x) if -_VALUE_LIMIT <= x <= _VALUE_LIMIT else None


@dataclass(frozen=True)
class Member:
    """One component market as the reading math sees it."""

    market_id: int
    venue: str
    title: str
    price: float | None       # last price 0–1, None = no snapshot yet
    volume_24h: float | None   # for the cross-venue liquidity fold
    price_at: datetime | None = None  # when `price` was last observed


def _contemporaneous(members: list[Member]) -> list[Member]:
    """Drop quotes far older than the freshest one on the same question.

    A survival ladder is only a distribution if its strikes were priced at
    roughly the same moment. ForecastEx leaves untraded strikes sitting for
    days, and the stale ones cross the live ones — Q4 2026 priced P(growth >
    1%) at 0.32 an hour ago while a 69-hour-old quote still claimed P(growth >
    1.5%) = 0.46. `_e_growth` used to clamp that inversion away silently, which
    turned incoherent input into a confident-looking number.

    Relative rather than absolute: the freshest quote always survives, so a
    slow-polling venue can never lose its anchor, and no assumption about ingest
    cadence is baked in. Unstamped quotes are kept — nothing to judge them on.
    """
    stamped = [m.price_at for m in members if m.price is not None and m.price_at]
    if not stamped:
        return members
    floor = max(stamped) - _MAX_QUOTE_SKEW
    return [m for m in members if m.price_at is None or m.price_at >= floor]


def _isotonic_nonincreasing(values: list[float]) -> list[float]:
    """Least-absolute-deviation non-increasing fit (pool-adjacent-violators on
    block medians), so one bad strike cannot drag the rest of the ladder.

    The ladder was made monotone with a running minimum, which lets the lowest
    price win against every strike above it: ForecastEx Q4 2026 printed P(>0.5%)
    at 1¢ between P(>-0.5%) = 0.87 and P(>1%) = 0.59, the running minimum set
    every higher strike to 0.01 and the venue read -0.47%. A least-squares fit
    (block means) still averages the print in (+0.25%). With medians a lone
    print is outvoted by its neighbours (+0.58%), while a one-against-one
    disagreement is split, since nothing says which side is wrong.
    """
    blocks: list[list[float]] = []
    for v in values:
        blocks.append([v])
        while len(blocks) > 1 and statistics.median(blocks[-2]) < statistics.median(blocks[-1]):
            tail = blocks.pop()
            blocks[-1].extend(tail)
    return [statistics.median(b) for b in blocks for _ in b]


def _e_growth(readings: list[tuple[str, float, float]]) -> float | None:
    """E[growth %] for one (anchor, venue). `readings` = [(kind, x, price)].

    Buckets take priority (a direct PMF); otherwise a survival ladder of ≥3
    priced thresholds. None when neither is usable.

    A *lone* bucket is not a PMF and is deliberately unreadable: `Σx·p / Σp`
    over one point collapses to that point's midpoint whatever the price, so a
    3%-likely "negative growth" contract would assert E = −0.5% exactly as hard
    as a 97%-likely one. Without the complement there is no expectation to read
    — the venue sits out the fold (it still shows in the badges / contract
    count) rather than pinning the anchor to one stray midpoint.
    """
    bkt = sorted((x, p) for k, x, p in readings if k == "bkt")
    thr = sorted((x, p) for k, x, p in readings if k == "thr")
    if len(bkt) >= _MIN_PMF_POINTS and len(bkt) >= len(thr):
        tot = sum(p for _, p in bkt)
        return sum(x * p for x, p in bkt) / tot if tot > 0 else None
    if len(thr) >= _MIN_SURVIVAL_POINTS:
        xs = [x for x, _ in thr]
        s = _isotonic_nonincreasing([p for _, p in thr])
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        half = (sum(gaps) / len(gaps)) / 2.0 if gaps else 0.5
        e = (1 - s[0]) * (xs[0] - half) + s[-1] * (xs[-1] + half)
        for i in range(len(xs) - 1):
            e += (s[i] - s[i + 1]) * ((xs[i] + xs[i + 1]) / 2.0)
        return e
    return None


def sentiment(e_growth: float | None) -> float | None:
    """Map E[growth %] → 0–100 economic-expectation sentiment (sigmoid, centred
    on the neutral baseline)."""
    if e_growth is None:
        return None
    return round(100.0 / (1.0 + math.exp(-SENSITIVITY * (e_growth - NEUTRAL_BASELINE))), 2)


_REGIMES: list[tuple[float, str]] = [
    (20.0, "Recession fear"),
    (40.0, "Pessimistic"),
    (60.0, "Neutral"),
    (80.0, "Optimistic"),
    (float("inf"), "Booming"),
]


def regime_for(score: float | None) -> str:
    if score is None:
        return "No signal"
    return next(label for hi, label in _REGIMES if score < hi)


def _period_order(period: str) -> tuple[int, int]:
    """'2026Q2' → (2026, 2); '2026' → (2026, 0). Sorts annual before its quarters."""
    year = int(period[:4])
    q = int(period[5]) if "Q" in period else 0
    return year, q


@dataclass(frozen=True)
class Anchor:
    period: str
    horizon: str          # 'annual' | 'quarter'
    label: str
    e_growth: float       # liquidity-folded across venues
    sentiment: float
    n_contracts: int
    venues: list[str]
    volume_24h: float | None = None  # summed real-money 24h book across venues
    # The contracts counted above, by id. The board is not an engine index, so
    # it writes no `ts_index_scores` row and has no `component_evaluation_ids`
    # lineage — this is the only way anything downstream can tell WHICH markets
    # a reading is made of, which /landing/stats needs to dedupe its
    # platform-wide contract count against the indexes that do have lineage.
    market_ids: tuple[int, ...] = ()


def _build_anchor(period: str, horizon: str, label: str, members: list[Member]) -> Anchor | None:
    """Fold one anchor's members (across venues) into a single E[growth]."""
    members = _contemporaneous(members)
    by_venue: dict[str, list[tuple[str, float, float]]] = {}
    ids: list[int] = []
    for m in members:
        if m.price is None:
            continue
        r = parse_reading(m.title)
        if r is None:
            continue
        by_venue.setdefault(m.venue, []).append((r[0], r[1], m.price))
        ids.append(m.market_id)

    if not by_venue:
        return None
    # Every venue with contracts on this question is shown (exchange badges +
    # contract count); the E-fold weights only the venues whose ladder actually
    # reads (a thin venue like a 2-contract manifold still shows, just can't
    # move the number).
    venues = sorted(by_venue)
    n_contracts = sum(len(r) for r in by_venue.values())

    es: list[float] = []
    weights: list[float] = []
    for venue, readings in by_venue.items():
        # Play-money is DISPLAYED (it stays in `by_venue` → the anchor's venue
        # chips + contract count) but NEVER enters the E-fold: site credits aren't
        # dollars (manifold mana ~9,000 vs ForecastEx ~$40 would hand it 99.6% of
        # the weight), and the money-type rule is play-money = display-only, never
        # scored. Skip it before es/weights.
        if venue in PLAY_MONEY_VENUES:
            continue
        e = _e_growth(readings)
        if e is None:
            continue
        es.append(e)
        # Liquidity weight = summed 24h volume of that venue's priced members,
        # floored so thin venues weigh equally (see `_MIN_FOLD_WEIGHT`).
        vol = sum(m.volume_24h or 0.0 for m in members if m.venue == venue)
        weights.append(max(vol, _MIN_FOLD_WEIGHT))
    if not es:
        return None
    e_fold = sum(e * w for e, w in zip(es, weights, strict=False)) / sum(weights)
    # Real-money 24h book for the card's "$X 24h vol" — play-money is site
    # credits, not dollars, so it stays out of the displayed figure (same rule
    # the liquidity fold above uses).
    volume = sum(m.volume_24h or 0.0 for m in members if m.venue not in PLAY_MONEY_VENUES)
    return Anchor(
        period=period,
        horizon=horizon,
        label=label,
        e_growth=round(e_fold, 2),
        sentiment=sentiment(e_fold),
        n_contracts=n_contracts,
        venues=venues,
        volume_24h=round(volume, 2),
        market_ids=tuple(ids),
    )


def build_board(
    members: list[Member], *, index_id: str = "us-gdp-expectation", min_year: int | None = None
) -> dict:
    """Group US GDP markets into (horizon, period) anchors, read each anchor's
    E[growth], and assemble the board.

    Headline = the nearest *annual* anchor's sentiment (the current
    whole-economy expectation, liquid + cross-venue). term_structure = the
    annual anchors oldest→newest (the market's expected growth path). anchors =
    every anchor for drill-down.

    `min_year` drops periods before it — a forward-looking expectation index
    ignores stale past-year markets that haven't resolved (e.g. a lingering 2025
    contract must not become the headline). Defaults to the current UTC year.
    """
    if min_year is None:
        min_year = datetime.now(UTC).year

    grouped: dict[str, tuple[str, str, list[Member]]] = {}
    for m in members:
        if not is_us(m.title):
            continue
        a = anchor_of(m.title)
        if a is None:
            continue
        horizon, period, label = a
        if int(period[:4]) < min_year:  # drop past periods
            continue
        grouped.setdefault(period, (horizon, label, []))[2].append(m)

    anchors: list[Anchor] = []
    for period, (horizon, label, ms) in grouped.items():
        built = _build_anchor(period, horizon, label, ms)
        if built is not None:
            anchors.append(built)
    anchors.sort(key=lambda a: _period_order(a.period))

    annual = [a for a in anchors if a.horizon == "annual"]
    # Headline = nearest annual anchor (smallest year), the liquid current read.
    headline = annual[0] if annual else (anchors[0] if anchors else None)

    return {
        "index_id": index_id,
        "score": headline.sentiment if headline else None,
        "e_growth": headline.e_growth if headline else None,
        "regime": regime_for(headline.sentiment if headline else None),
        "headline_period": headline.period if headline else None,
        "baseline": NEUTRAL_BASELINE,
        "sensitivity": SENSITIVITY,
        "n_contracts": sum(a.n_contracts for a in anchors),
        "n_anchors": len(anchors),
        "market_ids": sorted({mid for a in anchors for mid in a.market_ids}),
        "term_structure": [
            {"period": a.period, "e_growth": a.e_growth, "sentiment": a.sentiment,
             "n_contracts": a.n_contracts, "venues": a.venues}
            for a in annual
        ],
        "anchors": [
            {"period": a.period, "horizon": a.horizon, "label": a.label,
             "e_growth": a.e_growth, "sentiment": a.sentiment,
             "n_contracts": a.n_contracts, "venues": a.venues,
             "volume_24h": a.volume_24h}
            for a in anchors
        ],
    }
