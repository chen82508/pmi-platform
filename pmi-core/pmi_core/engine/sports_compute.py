"""sports_compute — pre-compute the Sports vertical boards from the game tags.

Reads `core_market_game_tag` (written by the tag-games LLM job + the deterministic
tag-sports-kalshi / tag-sports-polymarket / tag-gemini taggers) joined to live
prices, and folds markets into cross-venue boards. Returns board payloads the
`sports-snapshot` job writes to `ts_page_cache`; the API only ever `read_latest`s
them, so there is NO per-request compute.

Per sport the board carries: an OUTRIGHT leaderboard (MLB World Series / NFL Super
Bowl — each team's title probability across exchanges with a cross-exchange gap),
this slate's GAME cards (moneyline + run-line/spread + total, folded per venue),
and — MLB only — the live playoff bracket derived from the game matchups. A browse
feed: market-implied odds, no 0-100 score, no LLM at serve time.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.engine import mlb_reference, nfl_reference
from pmi_core.models import CoreMarket, CoreMarketGameTag

# Money-type per venue (same split the rest of the platform uses): play-money is
# SHOWN but kept out of the consensus, real-money drives it.
_PLAY_MONEY = {"manifold", "metaculus"}

# Partial-game markets (first-half / quarter lines) a game card must NOT fold
# into its full-game spread/total — "2H O/U 5.5", "1Q Spread", "4Q", etc.
_PERIOD_RE = re.compile(r"\b[1-4][HQ]\b|\b(1st|2nd|3rd|4th)\b|half|quarter", re.IGNORECASE)

# MLB's 30 teams, canonical names — the clean whitelist. The LLM normalises to
# these, so membership drops mis-tags ("Oklahoma") and non-team group/parlay bets
# in one check. (Other sports get their own set / a teams table later.)
_MLB_TEAMS = frozenset({
    "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
    "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
    "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
    "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
    "Minnesota Twins", "New York Mets", "New York Yankees", "Athletics",
    "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
    "San Francisco Giants", "Seattle Mariners", "St. Louis Cardinals",
    "Tampa Bay Rays", "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
})

# League split — used to group playoff series into the AL / NL halves of the
# bracket. Membership in one of these also IS the MLB-team check for matchups.
_AL_TEAMS = frozenset({
    "Baltimore Orioles", "Boston Red Sox", "New York Yankees", "Tampa Bay Rays",
    "Toronto Blue Jays", "Chicago White Sox", "Cleveland Guardians", "Detroit Tigers",
    "Kansas City Royals", "Minnesota Twins", "Houston Astros", "Los Angeles Angels",
    "Athletics", "Seattle Mariners", "Texas Rangers",
})
_NL_TEAMS = frozenset(_MLB_TEAMS - _AL_TEAMS)

# A league's second team below this World Series % means its LCS is over and the
# other team has advanced — the signal the bracket uses to show a pennant winner.
_ELIMINATED_WS_PCT = 2.0


def _fold_competitor(venue_prices: dict[str, tuple[float, float | None, str | None]]) -> dict:
    """One team's cross-venue reading from ONE price per venue: per-venue P(win)
    + the contract link, a real-money volume-weighted consensus, and the
    real-money max-min gap. Play-money is shown but never drives the consensus or
    the gap (it's noisy user-generated pricing)."""
    venues = [
        {"venue": v, "pct": round(p * 100, 1), "play_money": v in _PLAY_MONEY, "url": url}
        for v, (p, _vol, url) in venue_prices.items()
    ]
    real = [(p, vol) for v, (p, vol, _url) in venue_prices.items() if v not in _PLAY_MONEY]
    if real:
        wsum = sum((vol or 0.0) for _p, vol in real)
        consensus = (
            sum(p * (vol or 0.0) for p, vol in real) / wsum
            if wsum > 0
            else sum(p for p, _ in real) / len(real)
        )
        real_p = [p for p, _ in real]
        gap = round((max(real_p) - min(real_p)) * 100, 1)
    else:  # play-money only — show it, consensus is its (single folded) price
        consensus = sum(p for _v, (p, _vol, _url) in venue_prices.items()) / len(venue_prices)
        gap = 0.0
    return {
        "consensus_pct": round(consensus * 100, 1),
        "gap_pct": gap,
        "venues": sorted(venues, key=lambda x: -x["pct"]),
        "has_real_money": bool(real),
    }


def _slug(name: str) -> str:
    """Team name → URL slug (e.g. 'St. Louis Cardinals' → 'st-louis-cardinals')."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _canon_round(event: str | None, market_type: str) -> str:
    """One MLB round, one label. The LLM emits a different `event` string per venue
    for the same round ("NLCS" / "Championship Series" / "2026 National League
    Championship Series"), which otherwise scatters one real question across many
    groups on the team page. Fold them onto canonical rounds (most-specific first)."""
    if market_type == "prop":
        return "Props"
    e = (event or "").lower()
    if any(k in e for k in ("nlcs", "alcs", "pennant", "championship series",
                            "national league champ", "american league champ")):
        return "League Championship"
    # "pro baseball championship" is Kalshi's World Series wording. Match it
    # explicitly rather than a bare `endswith("championship")`, which would swallow
    # "Division Championship" (ordered before the Division branch below).
    if "world series" in e or "pro baseball championship" in e:
        return "World Series"
    if "division series" in e or "nlds" in e or "alds" in e:
        return "Division Series"
    if "wild card" in e:
        return "Wild Card"
    if any(k in e for k in ("mvp", "cy young", "rookie of", "silver slugger", "award")):
        return "Awards"
    return (event or "Other").strip()


@dataclass(frozen=True)
class KalshiSeries:
    round: str
    team: str  # the side this market prices
    opponent: str


# KXMLBSERIES-26TBMILWS-MIL: season, both teams, round code, priced side. Kalshi
# titles every LCS *and* the World Series "Championship Series winner", so the
# LLM's event label can't tell them apart; the round code can.
_KX_SERIES_RE = re.compile(r"^KXMLBSERIES-\d{2}([A-Z]+?)(WS|ALCS|NLCS|ALDS|NLDS|WC)-([A-Z]+)$")
_KX_SERIES_ROUND = {
    "WS": "World Series", "ALCS": "League Championship", "NLCS": "League Championship",
    "ALDS": "Division Series", "NLDS": "Division Series", "WC": "Wild Card",
}


def _kalshi_series(ext: str | None) -> KalshiSeries | None:
    m = _KX_SERIES_RE.match(ext or "")
    if not m:
        return None
    pair, code, side = m.groups()
    other = pair[len(side):] if pair.startswith(side) else pair[: -len(side)] if pair.endswith(side) else None
    codes = mlb_reference._ABBREV_TO_TEAM
    if other is None or side not in codes or other not in codes:
        return None
    return KalshiSeries(_KX_SERIES_ROUND[code], codes[side], codes[other])


# A Kalshi series market this far gone (or settled YES) means the series is over,
# even while Kalshi takes days to stamp it (the 2026 ALCS sat at 92% unsettled
# with Tampa Bay already in the World Series).
_SERIES_DECIDED = 0.97


async def _decided_series(session: AsyncSession) -> dict[frozenset, tuple[str, str]]:
    """{pairing: (round, winner)} for this postseason's finished Kalshi series."""
    rows = (
        await session.execute(
            select(CoreMarket.external_id, CoreMarket.last_price, CoreMarket.resolution)
            .join(CoreMarketGameTag, CoreMarketGameTag.market_id == CoreMarket.id)
            .where(
                CoreMarketGameTag.sport == "mlb",
                CoreMarket.venue == "kalshi",
                CoreMarket.external_id.like("KXMLBSERIES-%"),
                CoreMarket.closes_at >= datetime.now(UTC) - timedelta(days=60),
            )
        )
    ).all()
    out: dict[frozenset, tuple[str, str]] = {}
    for ext, price, resolution in rows:
        s = _kalshi_series(ext)
        if s is None:
            continue
        won = resolution == "YES" or (resolution is None and price is not None and float(price) >= _SERIES_DECIDED)
        if won:
            out[frozenset({s.team, s.opponent})] = (s.round, s.team)
    return out


# A decided-but-unsettled market goes silent: a finished round (Kalshi is slow to
# stamp `resolved`) stops trading, so its price freezes at the last trade (an
# eliminated "Division Series" stuck at 94% with a dead 0.03/0.97 book) while live
# rounds trade every minute. No trade in this many days ⇒ treat it as dead and hide.
_STALE_CONTRACT_DAYS = 3


async def _team_contracts(
    session: AsyncSession, sport: str, whitelist: frozenset, watch: set[int] | None = None
) -> dict[str, list[dict]]:
    """Every live, priced market per team (title / pennant / division / awards /
    props) for the team drill-down — keyed by competitor, one row per
    (canonical round, type, venue) folding the LLM's duplicate event labels. Each
    kept contract's market_id (carried alongside its dict) goes into ``watch``."""
    fresh_since = datetime.now(UTC) - timedelta(days=_STALE_CONTRACT_DAYS)
    rows = (
        await session.execute(
            select(
                CoreMarketGameTag.market_id,
                CoreMarketGameTag.competitor,
                CoreMarketGameTag.event,
                CoreMarketGameTag.market_type,
                CoreMarket.venue,
                CoreMarket.last_price,
                CoreMarket.last_volume_24h,
                CoreMarket.url,
                CoreMarket.external_id,
            )
            .join(CoreMarket, CoreMarket.id == CoreMarketGameTag.market_id)
            .where(
                CoreMarketGameTag.sport == sport,
                CoreMarketGameTag.competitor.is_not(None),
                ~CoreMarketGameTag.event.ilike("%2023%"),
                ~CoreMarketGameTag.event.ilike("%2024%"),
                ~CoreMarketGameTag.event.ilike("%2025%"),
                ~CoreMarketGameTag.event.ilike("%2027%"),  # next-season futures
                ~CoreMarketGameTag.event.ilike("%2028%"),
                # Kalshi series-STRUCTURE markets ("series goes N games" / exact
                # score / series spread) are LLM-mis-tagged as the team winning the
                # series; they add rows at nonsense prices. Drop them (the plain
                # KXMLBSERIES "wins the series" market stays).
                ~CoreMarket.external_id.like("KXMLBSERIESSCORE%"),
                ~CoreMarket.external_id.like("KXMLBSERIESSPREAD%"),
                CoreMarket.last_price.is_not(None),
                CoreMarket.resolved_at.is_(None),
                CoreMarket.last_price_at >= fresh_since,  # drop decided/dead books
            )
        )
    ).all()
    by_comp: dict[str, list[tuple[dict, int]]] = {}
    for mid, competitor, event, mtype, venue, price, vol, url, ext in rows:
        if competitor not in whitelist:
            continue
        series = _kalshi_series(ext)
        by_comp.setdefault(competitor, []).append((
            {
                "event": series.round if series else _canon_round(event, mtype),
                "type": mtype, "venue": venue,
                "pct": round(float(price) * 100, 1),
                "vol": float(vol) if vol is not None else None, "url": url,
            },
            mid,
        ))
    out: dict[str, list[dict]] = {}
    for comp, cs in by_comp.items():
        seen: set = set()
        kept: list[tuple[dict, int]] = []
        # Most-traded first (price as the tiebreak), so the row kept per (round,
        # type, venue) is the canonical market for that question on that book, not a
        # thin near-duplicate. The price tiebreak matters where a venue reports no
        # 24h volume (kalshi / forecastex store NULL), so the whole group sorts
        # equal on volume alone and would otherwise keep an arbitrary row.
        for c, mid in sorted(cs, key=lambda cm: (-(cm[0]["vol"] or 0.0), -cm[0]["pct"])):
            key = (c["event"], c["type"], c["venue"])
            if key in seen:
                continue
            seen.add(key)
            kept.append((c, mid))
            if len(kept) == 40:
                break
        if watch is not None:
            watch.update(mid for _, mid in kept)
        out[comp] = [c for c, _ in kept]
    return out


async def _outright_series_board(
    session: AsyncSession, sport: str, round_match: Callable[[str | None], bool],
    whitelist: frozenset, label: str,
    contracts_by_team: dict[str, list[dict]] | None = None,
    watch: set[int] | None = None,
) -> dict:
    """A championship-futures board (World Series, Super Bowl, …): every team's
    cross-venue consensus P(win title), ranked. ``round_match`` decides which
    event labels are this round — folding every venue's wording together (Kalshi
    calls the World Series "Pro Baseball Championship"), the same way the team
    drill-down does, so a book isn't dropped just for naming the round differently.
    Whitelist membership drops the mis-tags, parlay legs and group bets the LLM
    emits. The one market kept per (team, venue) is what the board shows; its id
    goes into ``watch`` only when the board actually renders."""
    contracts_by_team = contracts_by_team or {}
    fresh_since = datetime.now(UTC) - timedelta(days=_STALE_CONTRACT_DAYS)
    rows = (
        await session.execute(
            select(
                CoreMarketGameTag.market_id,
                CoreMarketGameTag.competitor,
                CoreMarketGameTag.event,
                CoreMarket.venue,
                CoreMarket.last_price,
                CoreMarket.last_volume_24h,
                CoreMarket.url,
            )
            .join(CoreMarket, CoreMarket.id == CoreMarketGameTag.market_id)
            .where(
                CoreMarketGameTag.sport == sport,
                CoreMarketGameTag.market_class == "outright",
                CoreMarketGameTag.market_type == "series",
                ~CoreMarketGameTag.event.ilike("%2024%"),
                ~CoreMarketGameTag.event.ilike("%2025%"),  # current season only
                ~CoreMarketGameTag.event.ilike("%2027%"),
                ~CoreMarketGameTag.event.ilike("%2028%"),
                ~CoreMarketGameTag.event.ilike("%worst%"),  # drops combo/prop junk
                ~CoreMarketGameTag.event.ilike("%margin%"),
                ~CoreMarketGameTag.event.ilike("%exact%"),
                CoreMarket.last_price.is_not(None),
                CoreMarket.resolved_at.is_(None),
                CoreMarket.last_price_at >= fresh_since,  # drop decided/dead books
            )
        )
    ).all()

    by_team: dict[str, dict[str, tuple[float, float | None, str | None]]] = {}
    kept_mid: dict[str, dict[str, int]] = {}  # the id of each slot's kept market
    for mid, competitor, event, venue, price, vol, url in rows:
        if competitor not in whitelist or not round_match(event):  # drops mis-tags, wrong round
            continue
        p = float(price)
        v = float(vol) if vol is not None else None
        slot = by_team.setdefault(competitor, {})
        # One market per venue — the most-traded, to dedupe near-duplicate tags.
        cur = slot.get(venue)
        if cur is None or (v or 0.0) > (cur[1] or 0.0):
            slot[venue] = (p, v, url)
            kept_mid.setdefault(competitor, {})[venue] = mid

    teams = [
        {
            "competitor": team, "slug": _slug(team),
            "contracts": contracts_by_team.get(team, []),
            **_fold_competitor(vp),
        }
        for team, vp in by_team.items()
    ]
    # Drop eliminated teams carried only by a near-zero play-money market — in the
    # postseason every knocked-out team still has a lingering manifold "0%" contract,
    # which would otherwise list all 30 teams on a 4-team race. A real-money price
    # (however small) or a non-trivial play-money one keeps the team.
    teams = [t for t in teams if t["has_real_money"] or t["consensus_pct"] >= 1.0]
    teams.sort(key=lambda t: (t["has_real_money"], t["consensus_pct"]), reverse=True)
    # Only a headline board when a real-money venue prices it — a board of only
    # play-money noise (e.g. a lone manifold market) would read as false signal.
    if not any(t["has_real_money"] for t in teams):
        return {"event": label, "kind": "outright", "teams": [], "n_teams": 0}
    if watch is not None:
        watch.update(mid for vm in kept_mid.values() for mid in vm.values())
    return {"event": label, "kind": "outright", "teams": teams, "n_teams": len(teams)}


def _playoff_bracket(ws_teams: list[dict]) -> dict | None:
    """The League Championship + World Series bracket, derived from the World Series
    board (the final four, two teams per league). When a league's second team has
    collapsed below ``_ELIMINATED_WS_PCT`` the series is over and the survivor is
    shown advanced to the World Series; otherwise the two are a live LCS matchup.
    The WS slot forms from the two pennant winners as each league decides.

    Returns None outside that stage — in the regular season / earlier rounds the
    field isn't a concentrated final four, so the module self-removes. (Deriving the
    bracket from the WS board, not single games, is what lets an advanced team stay
    on the tree after its series resolves and its games drop out.)
    """
    def team(t: dict, **flags: bool) -> dict:
        return {
            "competitor": t["competitor"],
            "short": mlb_reference.short_name(t["competitor"]),  # fan nickname
            "ws_pct": t["consensus_pct"],
            "url": t["venues"][0]["url"] if t.get("venues") else None,
            **flags,
        }

    by_league: dict[str, list[dict]] = {"AL": [], "NL": []}
    for t in ws_teams:
        by_league["AL" if t["competitor"] in _AL_TEAMS else "NL"].append(t)
    for lg in by_league:
        by_league[lg].sort(key=lambda t: t["consensus_pct"] or 0.0, reverse=True)

    # Postseason gate: a narrow, concentrated field (the final four or fewer share
    # ~all the probability). In the regular season championship odds spread across
    # 15+ teams, so this stays False and no bracket renders.
    alive = [t for t in ws_teams if (t["consensus_pct"] or 0.0) >= _ELIMINATED_WS_PCT]
    if not (1 <= len(alive) <= 4 and sum(t["consensus_pct"] or 0.0 for t in alive) >= 90):
        return None

    champions: dict[str, dict | None] = {"AL": None, "NL": None}
    matchups: dict[str, dict | None] = {"AL": None, "NL": None}
    for lg in ("AL", "NL"):
        pair = by_league[lg][:2]  # the two LCS participants, strongest first
        if not pair:
            continue
        top = pair[0]
        second = pair[1] if len(pair) > 1 else None
        decided = second is None or (second["consensus_pct"] or 0.0) < _ELIMINATED_WS_PCT
        if decided:
            champions[lg] = team(top, advanced=True)
        teams = [team(top, advanced=decided)]
        if second is not None:
            teams.append(team(second, eliminated=decided))
        matchups[lg] = {"teams": teams, "decided": decided}

    both_decided = bool(champions["AL"] and champions["NL"])
    return {
        "round_label": "World Series" if both_decided else "League Championship",
        "al": matchups["AL"],
        "nl": matchups["NL"],
        "world_series": {"al": champions["AL"], "nl": champions["NL"]},
    }


def _fold_markets(markets: list[dict]) -> dict:
    """Fold a set of markets that all price the SAME outcome into one cross-venue
    reading (one price per venue, the most-traded). `markets`: dicts with venue /
    price / vol / url."""
    by_venue: dict[str, tuple[float, float | None, str | None]] = {}
    for mk in markets:
        cur = by_venue.get(mk["venue"])
        if cur is None or (mk["vol"] or 0.0) > (cur[1] or 0.0):
            by_venue[mk["venue"]] = (mk["price"], mk["vol"], mk["url"])
    return _fold_competitor(by_venue)


def _fold_moneyline(mks: list[dict], home: str, away: str) -> tuple[dict[str, float], list]:
    """{team: win %} + per-venue reads, folded on ONE axis: every moneyline leg
    becomes P(home wins) (an away-side quote is complemented) before the per-venue
    pick. Folding each side separately left a venue that prices only the away side
    (Polymarket's outcome-0 market) out of the home read and the venue list, and let
    the two sides drift off summing to 100. A side-less leg is P(its own home)."""
    legs = []
    for k in mks:
        if k["type"] != "moneyline":
            continue
        subject = k["side"] if k["side"] is not None else k["home"]
        if subject == home:
            legs.append(k)
        elif subject == away:
            legs.append({**k, "price": 1.0 - k["price"]})
    if not legs:
        return {}, []
    folded = _fold_markets(legs)
    home_pct = folded["consensus_pct"]
    return {home: home_pct, away: round(100.0 - home_pct, 1)}, folded["venues"]


# MLB full-game totals sit ~6–13 runs; team/inning totals (~2–5) that slip through
# tagging are dropped by this floor (the NFL analogue is the ≥30 game-total floor).
_MLB_TOTAL_FLOOR = 6.0


async def _mlb_games(session: AsyncSession, watch: set[int] | None = None) -> list[dict]:
    """This series' MLB games as cross-venue cards — moneyline folded across
    exchanges, with the run line / game total when a book prices them. The MLB
    counterpart of `_nfl_games`; the real-money single-game action is Kalshi's
    KXMLBGAME winner (deterministically tagged by tag-sports-kalshi).

    Both teams must be real MLB (drops the NPB/KBO games the LLM tags as mlb). A
    postseason matchup plays several games in a week, so games are keyed by
    (pairing, date) — one card per actual game — and the greedy one-per-team keeps
    each team's NEXT game rather than folding a whole series into one price.
    """
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(
                CoreMarketGameTag.market_id,
                CoreMarketGameTag.home_team,
                CoreMarketGameTag.away_team,
                CoreMarketGameTag.market_type,
                CoreMarketGameTag.line_value,
                CoreMarketGameTag.side,
                CoreMarketGameTag.game_date,
                CoreMarketGameTag.resolved_via,
                CoreMarket.venue,
                CoreMarket.last_price,
                CoreMarket.last_volume_24h,
                CoreMarket.url,
                CoreMarket.title,
            )
            .join(CoreMarket, CoreMarket.id == CoreMarketGameTag.market_id)
            .where(
                CoreMarketGameTag.sport == "mlb",
                CoreMarketGameTag.market_class == "game",
                CoreMarketGameTag.game_date >= now - timedelta(days=1),
                CoreMarketGameTag.game_date <= now + timedelta(days=10),
                CoreMarket.resolved_at.is_(None),
                # Deterministically-tagged games only (kalshi KXMLBGAME ticker,
                # polymarket slug). The LLM tagger guesses sport/teams/date off
                # one-team kalshi titles — e.g. an NFL "Arizona vs Los Angeles"
                # market tagged mlb Dodgers/Diamondbacks — so an LLM tag can invent a
                # game that isn't scheduled. Gemini models every hypothetical series
                # game (both orderings, no side), so the venue exclusion stays on top.
                CoreMarketGameTag.resolved_via.in_(("ticker", "slug")),
                CoreMarket.venue.not_in(("gemini", "manifold", "metaculus")),
            )
        )
    ).all()

    # Bucket markets into one game per (unordered pairing, calendar date). A series
    # plays MIL@LAD on consecutive days — each is its own game, never folded.
    games: dict[tuple, dict] = {}
    for mid, home, away, mtype, line, side, gdate, resolved_via, venue, price, vol, url, title in rows:
        if home not in _MLB_TEAMS or away not in _MLB_TEAMS:
            continue  # both teams must be real MLB — drops foreign-league mis-tags
        if _PERIOD_RE.search(title or ""):
            continue  # partial-game (e.g. "1st 5 Innings") must not fold into the full game
        key = (frozenset({home, away}), gdate.date() if gdate else None)
        g = games.setdefault(key, {"home": home, "away": away, "date": gdate, "mk": []})
        # The deterministic taggers (kalshi KXMLBGAME ticker, polymarket slug) know
        # the true away@home orientation; let them override an LLM-guessed flip.
        if resolved_via in ("ticker", "slug"):
            g["home"], g["away"] = home, away
        g["mk"].append({
            "id": mid, "type": mtype, "line": float(line) if line is not None else None,
            "side": side, "home": home, "away": away, "venue": venue,
            "price": float(price) if price is not None else None,
            "vol": float(vol) if vol is not None else None, "url": url,
        })

    cards: list[dict] = []
    mids_by_slug: dict[str, list[int]] = {}
    for m in games.values():
        home, away = m["home"], m["away"]
        mks = [k for k in m["mk"] if k["price"] is not None]  # fold priced only
        if not mks:
            continue
        ml, ml_venues = _fold_moneyline(mks, home, away)

        # Headline run line / total = the balanced quote (price nearest 50%).
        def _main(mk: dict) -> tuple:
            p = mk["price"] if mk["price"] is not None else 0.5
            return (abs(p - 0.5), -(mk["vol"] or 0.0))

        runlines = [k for k in mks if k["type"] == "spread" and k["line"] is not None and k["side"] in (home, away)]
        totals = [k for k in mks if k["type"] == "total" and k["line"] is not None and k["line"] >= _MLB_TOTAL_FLOOR]
        runline = None
        if runlines:
            hd = min(runlines, key=_main)
            fav = hd["side"] if hd["line"] <= 0 else (away if hd["side"] == home else home)
            line = hd["line"] if hd["line"] <= 0 else -hd["line"]
            runline = {"favorite": fav, "favorite_short": mlb_reference.short_name(fav), "line": round(line, 1)}
        total_hd = min(totals, key=_main) if totals else None

        if not ml and runline is None and total_hd is None:
            continue

        slug = f"{mlb_reference.abbrev(away).lower()}-{mlb_reference.abbrev(home).lower()}"
        mids_by_slug[slug] = [k["id"] for k in mks]
        cards.append({
            "slug": slug,
            "home": home, "home_short": mlb_reference.short_name(home),
            "away": away, "away_short": mlb_reference.short_name(away),
            "game_date": m["date"].isoformat() if m["date"] else None,
            "moneyline": {"home_pct": ml.get(home), "away_pct": ml.get(away), "venues": ml_venues, "implied": False},
            "spread": runline,
            "total": {"line": round(total_hd["line"], 1)} if total_hd else None,
            "venues": sorted({k["venue"] for k in mks}),
            "n_contracts": len(mks),
            "contracts": [
                {
                    "type": k["type"], "line": k["line"], "side": k["side"],
                    "pct": round(k["price"] * 100, 1), "volume_24h": k["vol"],
                    "venue": k["venue"], "url": k["url"],
                }
                for k in sorted(mks, key=lambda k: k["vol"] or 0.0, reverse=True)
            ],
        })

    # Greedy one-game-per-team, earliest first → each active team's NEXT game.
    cards.sort(key=lambda g: (g["game_date"] or "", -g["n_contracts"]))
    used: set[str] = set()
    kept = []
    for g in cards:
        if g["home"] in used or g["away"] in used:
            continue
        used.add(g["home"])
        used.add(g["away"])
        kept.append(g)
    if watch is not None:
        for g in kept:
            watch.update(mids_by_slug[g["slug"]])
    return kept


_ORIENTATION_RANK = {"slug": 2, "ticker": 1}


async def _nfl_games(session: AsyncSession, watch: set[int] | None = None) -> list[dict]:
    """This week's NFL games as cross-venue cards — the sportsbook showcase MLB
    lacks: moneyline + point spread + game total, each folded across exchanges.

    Both teams must be in the 32-team whitelist (drops the NCAA college games the
    LLM also tags as nfl). Matchups are seeded from any market naming both teams;
    single-sided spread/total markets (opponent un-tagged) attach by their known
    team. Headline spread/total = the most-traded line; the full ladder rides
    along in `contracts` for the game drill-down.
    """
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(
                CoreMarketGameTag.market_id,
                CoreMarketGameTag.home_team,
                CoreMarketGameTag.away_team,
                CoreMarketGameTag.market_type,
                CoreMarketGameTag.line_value,
                CoreMarketGameTag.side,
                CoreMarketGameTag.game_date,
                CoreMarketGameTag.resolved_via,
                CoreMarket.venue,
                CoreMarket.last_price,
                CoreMarket.last_volume_24h,
                CoreMarket.url,
                CoreMarket.title,
            )
            .join(CoreMarket, CoreMarket.id == CoreMarketGameTag.market_id)
            .where(
                CoreMarketGameTag.sport == "nfl",
                CoreMarketGameTag.market_class == "game",
                # This week's slate only (NFL = ~1 game/team/week) so a team lands
                # in one matchup, not several weeks folded together.
                CoreMarketGameTag.game_date >= now - timedelta(days=2),
                CoreMarketGameTag.game_date <= now + timedelta(days=8),
                CoreMarket.resolved_at.is_(None),
                # Drop the zero-volume modeled venues (same exclusion as the MLB
                # games board). Gemini prices every hypothetical matchup at a
                # confident-looking number, so a single stale Gemini leg would
                # otherwise surface as a 99% "game" with no real-money backing.
                # Unlike MLB we can't also require deterministic tags here —
                # Kalshi NFL spreads/totals ride the LLM title path, so a
                # resolved_via filter would gut the board. The 32-team
                # whitelist below already drops cross-sport mis-tags.
                CoreMarket.venue.not_in(("gemini", "manifold", "metaculus")),
            )
        )
    ).all()

    # Note: UN-priced markets are pulled too — a two-sided market (both teams)
    # seeds the matchup even when stale/unpriced (e.g. a dormant Polymarket
    # moneyline), so a game still surfaces off a priced Kalshi spread. Only
    # PRICED markets are ever folded into a reading.
    N = nfl_reference.TEAM_NAMES
    matchups: dict[frozenset, dict] = {}
    singles: list[dict] = []
    for mid, home, away, mtype, line, side, gdate, resolved_via, venue, price, vol, url, title in rows:
        # Drop partial-game markets (1H/2H/1Q…) tagged as game — they pollute the
        # full-game spread/total headline with half/quarter lines.
        if _PERIOD_RE.search(title or ""):
            continue
        mk = {
            "id": mid,
            "home": home, "away": away, "type": mtype,
            "line": float(line) if line is not None else None,
            "side": side, "date": gdate, "venue": venue,
            "price": float(price) if price is not None else None,
            "vol": float(vol) if vol is not None else None, "url": url,
        }
        if home in N and away in N:
            key = frozenset({home, away})
            m = matchups.setdefault(key, {"teams": (home, away), "date": gdate, "rank": -1, "mk": []})
            m["mk"].append(mk)
            # Away@home orientation and kickoff come from the most trustworthy tag:
            # the Polymarket slug (kickoff time) > the Kalshi ticker (date only) > an
            # LLM guess. First-row-wins once flipped LAC@LAR and dated it midnight.
            rank = _ORIENTATION_RANK.get(resolved_via, 0)
            if rank > m["rank"]:
                m["teams"], m["rank"] = (home, away), rank
                m["date"] = gdate or m["date"]
        elif (home in N or away in N) and mtype in ("spread", "total"):
            singles.append(mk)  # props never attach by one team (league-wide props did)

    for mk in singles:  # attach to the known team's matchup nearest in date
        t = mk["home"] if mk["home"] in N else mk["away"]
        cands = [m for m in matchups.values() if t in m["teams"]]
        if cands:
            nearest = min(cands, key=lambda m: abs((m["date"] - mk["date"]).total_seconds())
                          if m["date"] and mk["date"] else 0.0)
            nearest["mk"].append(mk)

    games = []
    mids_by_slug: dict[str, list[int]] = {}  # kept games' folded market ids, for watch
    for m in matchups.values():
        home, away = m["teams"]
        mks = [k for k in m["mk"] if k["price"] is not None]  # fold priced only
        if not mks:
            continue  # pairing exists but nothing priced to show yet
        ml, ml_venues = _fold_moneyline(mks, home, away)

        # Headline line = the balanced one (price nearest 50%) — the consensus
        # spread/total; volume is only a tiebreak (≈0 on fresh books).
        def _main(mk: dict) -> tuple:
            p = mk["price"] if mk["price"] is not None else 0.5
            return (abs(p - 0.5), -(mk["vol"] or 0.0))

        # The moneyline favorite anchors which spread is real (the tag conflates
        # game spreads with "team wins by over X" / team-total props).
        ml_fav = None
        if ml:
            ml_fav = max(ml, key=lambda t: ml[t])

        def _spread_fav(k: dict) -> str:
            """Team laying points for a spread quote (flip underdog +lines)."""
            return k["side"] if k["line"] <= 0 else (away if k["side"] == home else home)

        spreads = [
            k for k in mks
            if k["type"] == "spread" and k["line"] is not None and k["side"] in (home, away)
            and (ml_fav is None or _spread_fav(k) == ml_fav)  # favorite must agree with the ML
        ]
        # Full-game totals only: NFL game totals are ≥30; team/half/quarter
        # totals (~10–27) that slip through tagging are dropped here.
        totals = [k for k in mks if k["type"] == "total" and k["line"] is not None and abs(k["line"]) >= 30.0]
        spread = None
        if spreads:
            hd = min(spreads, key=_main)
            fav = _spread_fav(hd)
            line = hd["line"] if hd["line"] <= 0 else -hd["line"]
            spread = {"favorite": fav, "favorite_short": nfl_reference.short_name(fav), "line": round(line, 1)}
        total_hd = min(totals, key=_main) if totals else None

        # No moneyline market? Imply win % from the spread — NFL game margins are
        # ≈ Normal(0, 13.5 pts), so P(favorite wins) = Φ(points / 13.5). (A total
        # says nothing about who wins.) Flagged implied so the UI can mark it.
        ml_implied = False
        if not ml and spread is not None:
            z = abs(spread["line"]) / 13.5
            fav_wp = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
            dog = away if spread["favorite"] == home else home
            ml[spread["favorite"]] = round(fav_wp * 100, 0)
            ml[dog] = round((1.0 - fav_wp) * 100, 0)
            ml_implied = True

        # Something to show? (a game with only props folds to nothing useful.)
        if not ml and not spread and not total_hd:
            continue

        slug = f"{nfl_reference.abbrev(away).lower()}-{nfl_reference.abbrev(home).lower()}"
        mids_by_slug[slug] = [k["id"] for k in mks]  # every market folded into this game
        games.append({
            "slug": slug,
            "home": home, "home_short": nfl_reference.short_name(home),
            "away": away, "away_short": nfl_reference.short_name(away),
            "game_date": m["date"].isoformat() if m["date"] else None,
            "moneyline": {"home_pct": ml.get(home), "away_pct": ml.get(away), "venues": ml_venues, "implied": ml_implied},
            "spread": spread,
            "total": {"line": round(total_hd["line"], 1)} if total_hd else None,
            "venues": sorted({k["venue"] for k in mks}),
            "n_contracts": len(mks),
            # Full ladder for the game drill-down: every contract with its price.
            "contracts": [
                {
                    "type": k["type"], "line": k["line"], "side": k["side"],
                    "pct": round(k["price"] * 100, 1), "volume_24h": k["vol"],
                    "venue": k["venue"], "url": k["url"],
                }
                for k in sorted(mks, key=lambda k: k["vol"] or 0.0, reverse=True)
            ],
        })

    # Greedy one-game-per-team: richest-covered game wins, so a team's thin
    # duplicate (often a lone play-money market) drops out.
    games.sort(key=lambda g: (g["n_contracts"], g["game_date"] or ""), reverse=True)
    used: set[str] = set()
    kept = []
    for g in games:
        if g["home"] in used or g["away"] in used:
            continue
        used.add(g["home"])
        used.add(g["away"])
        kept.append(g)
    kept.sort(key=lambda g: g["game_date"] or "")
    if watch is not None:
        for g in kept:
            watch.update(mids_by_slug[g["slug"]])
    return kept


async def compute_sports(session: AsyncSession, sport: str = "mlb") -> tuple[dict, set[int]]:
    """One sport's vertical, pre-computed as data-driven modules that appear only
    when their data exists — so the SAME shape serves the regular season and the
    postseason. MLB: World Series board + live playoff bracket. NFL: this week's
    games (moneyline/spread/total) + Super Bowl futures.

    Returns ``(board, watch_ids)``: watch_ids is every market_id folded into the
    rendered board — what the snapshot stamps for the priority cron (sports has no
    score, so these markets never reach the cron's component lane).
    """
    watch: set[int] = set()
    if sport == "nfl":
        sb = await _outright_series_board(
            session, "nfl", lambda ev: "super bowl" in (ev or "").lower(),
            nfl_reference.TEAM_NAMES, "Super Bowl", watch=watch
        )
        games = await _nfl_games(session, watch=watch)
        board = {
            "sport": "nfl",
            "outrights": [sb] if sb["teams"] else [],
            "bracket": None,  # NFL regular season — no bracket
            "games": games,
        }
        return board, watch

    # MLB (default)
    mlb_contracts = await _team_contracts(session, "mlb", _MLB_TEAMS, watch=watch)
    ws = await _outright_series_board(
        session, "mlb", lambda ev: _canon_round(ev, "series") == "World Series",
        _MLB_TEAMS, "World Series", mlb_contracts, watch=watch
    )
    bracket = _playoff_bracket(ws["teams"])
    decided = await _decided_series(session)
    # A finished series leaves "if necessary" games priced until the venue voids
    # them (Polymarket's 2026 WS Game 6 sat at 57/43 for 7 hours after the final out).
    games = [g for g in await _mlb_games(session, watch=watch)
             if frozenset({g["home"], g["away"]}) not in decided]
    champion = next((w for rnd, w in decided.values() if rnd == "World Series"), None)
    board = {
        "sport": "mlb",
        "outrights": [ws] if ws["teams"] else [],
        "bracket": bracket,  # None in the regular season → no bracket module
        "games": games,
        "champion": {"competitor": champion, "short": mlb_reference.short_name(champion),
                     "event": "World Series"} if champion else None,
    }
    return board, watch
