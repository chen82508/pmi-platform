"""Esports board compute — the heavy fold + holdings + trend, shared by the
`esports-snapshot` worker (writes ts_page_cache) and the pmi-api route's
cache-miss fallback.

Every other vertical serves its page from a pre-computed ts_page_cache blob; this
module is what lets esports do the same. It returns plain JSON-serialisable dicts
(no pmi-api schema imports) so the worker can stash them and the route can wrap
them in its response models. Fold logic itself lives in `esports_matches`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pmi_core.engine import esports_matches as em
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.engine.markets_board import market_url
from pmi_core.models import CoreMarket, TsPriceSnapshot

# Per-game videogame tokens as they appear in a title. Kalshi/Robinhood say the
# full game name, Polymarket abbreviates ("LoL:").
GAME_TOKENS: dict[str, str] = {
    "cs2": "CS2|Counter-Strike",
    "valorant": "Valorant",
    "lol": "League of Legends|LoL",
}
ALL_GAMES_ALT = "|".join(GAME_TOKENS.values())


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_KALSHI_TICKER_RE = r"^KX(LOL|CS2|VALORANT)GAME-"


def winner_market_branches(game_alt: str) -> list[ColumnElement[bool]]:
    """The winner-market predicate per venue, one element per venue.

    They used to be OR-ed into ONE statement. With `venue = …` buried inside an
    OR the planner walked every open market of all three venues and detoasted
    `raw` on each row — ~360 s per esports snapshot, two thirds of its 11 min
    (2026-10-20). Run each branch as its own statement and every venue takes its
    own index path (the live (venue, closes_at) partial index, then the ticker
    regex / the small robinhood set / the title trigram index — 2-4 s in all);
    the union is the same rows because the branches are disjoint on `venue`."""
    group_item = CoreMarket.raw.op("->>")("groupItemTitle")
    return [
        # Kalshi's winner markets live under stable KX{GAME}GAME series; the
        # title no longer carries the matchup (teams come from yes_sub_title),
        # so identify by ticker prefix, not a title regex (2026-08 format change).
        and_(
            CoreMarket.venue == "kalshi",
            CoreMarket.external_id.op("~*")(_KALSHI_TICKER_RE),
        ),
        # Robinhood still uses the old "Will W win the A vs B <game> match?"
        # grammar (its parser is unchanged).
        and_(
            CoreMarket.venue == "robinhood",
            CoreMarket.title.op("~*")(rf"^Will .+ win the .+ vs\.? .+ ({game_alt}) match\??$"),
        ),
        and_(
            CoreMarket.venue == "polymarket",
            group_item == "Match Winner",
            CoreMarket.title.op("~*")(rf"^({game_alt}): .+ vs .+ \(BO[0-9]+\)"),
        ),
    ]


async def _execute_branches(session: AsyncSession, stmt: Select, branches: list) -> list:
    """`stmt` once per branch, rows concatenated (branches are venue-disjoint)."""
    rows: list = []
    for branch in branches:
        rows.extend((await session.execute(stmt.where(branch))).all())
    return rows


async def _winner_candidates(
    session: AsyncSession, game_alt: str, cutoff: datetime
) -> list[em.Mkt]:
    """Kalshi/Robinhood match-winner + Polymarket 'Match Winner' rows → Mkt tuples."""
    group_item = CoreMarket.raw.op("->>")("groupItemTitle")
    stmt = select(
        CoreMarket.id,
        CoreMarket.venue,
        CoreMarket.title,
        CoreMarket.external_id,
        # Kalshi groups winner legs on event_ticker; Robinhood on its eventId.
        func.coalesce(
            CoreMarket.raw.op("->>")("event_ticker"),
            CoreMarket.raw.op("->>")("eventId"),
        ),
        group_item,
        CoreMarket.last_price,
        CoreMarket.last_volume_24h,
        CoreMarket.closes_at,
        CoreMarket.url,
        CoreMarket.raw.op("->>")("rules_primary"),
        func.coalesce(
            CoreMarket.raw.op("->>")("occurrence_datetime"),
            CoreMarket.raw.op("->>")("gameStartTime"),
        ),
        CoreMarket.raw.op("->>")("yes_sub_title"),
    ).where(
        *live_market_conditions(),
        CoreMarket.last_price.is_not(None),
        CoreMarket.last_price_at >= cutoff,
    )
    return [
        em.Mkt(
            market_id=mid,
            venue=venue,
            title=title,
            external_id=external_id,
            event_key=event_key,
            group_item=grp,
            price=float(price) if price is not None else None,
            volume_24h=float(vol) if vol is not None else None,
            closes_at=closes_at,
            url=url,
            rules=rules,
            match_time=_parse_iso(match_raw),
            yes_sub=yes_sub,
        )
        for mid, venue, title, external_id, event_key, grp, price, vol, closes_at, url, rules, match_raw, yes_sub in (
            await _execute_branches(session, stmt, winner_market_branches(game_alt))
        )
    ]


def _holding_category(venue: str, title: str, group_item: str | None) -> str:
    """Label a contract by market type, for the holdings list badge."""
    g = (group_item or "").strip()
    if venue == "polymarket" and g:
        gl = g.lower()
        if "handicap" in gl:
            return "Handicap"
        mm = re.match(r"(?:map|game) (\d+)", gl)
        return f"Game {mm.group(1)}" if mm else g
    t = title.lower()
    m = re.search(r"win (?:map|game) (\d+)", t)
    if m:
        return f"Game {m.group(1)}"
    if "maps be played" in t:
        return "Total Maps"
    if "match?" in t or " match " in t or t.endswith(" wins") or t.endswith(" win"):
        return "Match Winner"
    return "Other"


_RH_WINNER = re.compile(r"^Will (.+?) win the ", re.I)


def _payout_side(venue: str, title: str, yes_sub: str | None) -> str | None:
    """The team a YES pays on, where the venue names it: Kalshi's
    ``yes_sub_title`` IS the team; Robinhood's title grammar names the winner
    leg. Polymarket group legs don't state a side per market → None."""
    if venue == "kalshi" and yes_sub:
        return yes_sub.strip() or None
    if venue == "robinhood":
        m = _RH_WINNER.match(title.strip())
        return m.group(1).strip() if m else None
    return None


async def _match_holdings(session: AsyncSession, match: dict, cutoff: datetime) -> list[dict]:
    """Every backing contract for a match (winner legs, per-map, total-maps,
    handicap) across exchanges — plain dicts (EsportsHolding shape)."""
    anchor = datetime.fromisoformat(match["starts_at"]) if match["starts_at"] else None
    # Related market types (per-map, total-maps, handicap) carry BOTH team names +
    # sit in the match's time window. New-format Kalshi winner legs only name ONE
    # team ("Maryville University wins"), so that heuristic misses them — OR in the
    # fold's own market_ids (the winner legs are already known good) to guarantee
    # they show as holdings.
    name_conds = [
        CoreMarket.title.ilike(f"%{match['team_a']}%"),
        CoreMarket.title.ilike(f"%{match['team_b']}%"),
    ]
    if anchor is not None:
        name_conds.append(CoreMarket.closes_at >= anchor - timedelta(hours=60))
        name_conds.append(CoreMarket.closes_at <= anchor + timedelta(hours=60))
    fold_ids = match.get("market_ids") or []
    conds = [
        *live_market_conditions(),
        CoreMarket.last_price.is_not(None),
        CoreMarket.last_price_at >= cutoff,
    ]
    stmt = select(
        CoreMarket.id,
        CoreMarket.venue,
        CoreMarket.title,
        CoreMarket.raw.op("->>")("groupItemTitle"),
        CoreMarket.last_price,
        CoreMarket.last_volume_24h,
        CoreMarket.url,
        CoreMarket.external_id,
        CoreMarket.raw,
        CoreMarket.raw.op("->>")("yes_sub_title"),
    ).where(*conds)
    # Two statements, not `(names AND window) OR id IN (…)`: the OR stopped the
    # planner using the title trigram index for the names and the PK for the
    # ids, so every match scanned the open-market set (~3 s × ~100 matches per
    # snapshot, 2026-10-20). The union is identical; dedupe on id.
    seen: set[int] = set()
    rows: list = []
    for stmt_part in holdings_stmts(stmt, name_conds, fold_ids):
        for row in (await session.execute(stmt_part)).all():
            if row[0] not in seen:
                seen.add(row[0])
                rows.append(row)
    holdings = [
        {
            "market_id": mid,
            "title": title,
            "venue": venue,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol) if vol is not None else None,
            "url": market_url(venue, external_id, raw, url),
            "category": _holding_category(venue, title, grp),
            # Which outcome YES pays — only where the venue states it
            # unambiguously (D14 payout side); None elsewhere, never guessed.
            "side": _payout_side(venue, title, yes_sub),
        }
        for mid, venue, title, grp, price, vol, url, external_id, raw, yes_sub in rows
    ]
    holdings.sort(key=lambda h: -(h["volume_24h"] or 0.0))
    return holdings


def holdings_stmts(stmt: Select, name_conds: list, fold_ids: list[int]) -> list[Select]:
    """The holdings query as one statement per OR arm (see `_match_holdings`)."""
    out = [stmt.where(and_(*name_conds))]
    if fold_ids:
        out.append(stmt.where(CoreMarket.id.in_(fold_ids)))
    return out


async def _match_trend(
    session: AsyncSession, match: dict, rows: list[em.Mkt], now: datetime, days: int
) -> dict:
    """The favourite's win % over time (hourly), oriented to the favourite —
    plain dict {favourite, days, points:[{date,value}]}."""
    points: list[dict] = []
    sel = em.favourite_market(rows, match)
    if sel is not None:
        market_id, price_is_fav = sel
        hour = func.date_trunc("hour", TsPriceSnapshot.snapshot_at)
        prows = (
            await session.execute(
                select(hour.label("t"), TsPriceSnapshot.last_price)
                .where(
                    TsPriceSnapshot.market_id == market_id,
                    TsPriceSnapshot.snapshot_at >= now - timedelta(days=days),
                    TsPriceSnapshot.snapshot_at <= now,
                    TsPriceSnapshot.last_price.isnot(None),
                )
                .distinct(hour)
                .order_by(hour, TsPriceSnapshot.snapshot_at.desc())
            )
        ).all()
        points = [
            {
                "date": t.isoformat(),
                "value": round((float(p) if price_is_fav else 1.0 - float(p)) * 100.0, 1),
            }
            for t, p in prows
        ]
    return {"favourite": match["favorite"], "days": days, "points": points}


# Winner-leg market ids carried by the last N esports boards — the live
# `matches[]` plus the `settled[]` list each board already shows. page-cache
# retention keeps 24 rows of a latest-only key, so 48 is "everything kept".
_RECENT_BOARD_IDS_SQL = text(
    """
    SELECT DISTINCT mid.value::bigint
    FROM (
        SELECT payload FROM ts_page_cache
        WHERE cache_key = 'esports-board'
        ORDER BY computed_at DESC
        LIMIT :limit_rows
    ) b,
    LATERAL jsonb_array_elements(
        coalesce(b.payload -> 'matches', '[]'::jsonb) || coalesce(b.payload -> 'settled', '[]'::jsonb)
    ) m,
    LATERAL jsonb_array_elements_text(coalesce(m -> 'market_ids', '[]'::jsonb)) mid
    """
)


def clean_market_ids(values: list) -> list[int]:
    """Sorted, de-duplicated ints out of whatever the boards carried."""
    ids: set[int] = set()
    for v in values:
        try:
            ids.add(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(ids)


async def _recent_board_market_ids(session: AsyncSession, limit_rows: int = 48) -> list[int]:
    rows = (await session.execute(_RECENT_BOARD_IDS_SQL, {"limit_rows": limit_rows})).scalars().all()
    return clean_market_ids(list(rows))


async def _settled_matches(session: AsyncSession, hours: int = 72) -> list[dict]:
    """Recently settled matches (D14): winner-pattern markets RESOLVED within
    ``hours``, folded with the same ``build_matches``, the winner read from the
    resolved YES leg (Kalshi ``yes_sub_title`` / Robinhood title grammar /
    Polymarket's single two-outcome market), with a pinned-price fallback.
    A match whose winner can't be derived ships without one — never guessed.

    Candidates are the winner legs the recent boards already carried (see
    `_recent_board_market_ids`), looked up by primary key. The venue/regex scan
    over core_markets this replaces had no index to lean on (`resolved_at` is
    unindexed) and cost 2-5 min per esports snapshot (2026-10-20). Every match
    that can settle was live on a recent board, and a settled one stays in
    `settled[]` for its window, so the boards' own ids are the candidate set;
    the one thing no longer found is a winner leg that resolved before any
    board snapshot ever listed it — a match the page never showed live."""
    now = datetime.now(UTC)
    candidate_ids = await _recent_board_market_ids(session)
    if not candidate_ids:
        return []
    group_item = CoreMarket.raw.op("->>")("groupItemTitle")
    stmt = select(
        CoreMarket.id,
        CoreMarket.venue,
        CoreMarket.title,
        CoreMarket.external_id,
        func.coalesce(
            CoreMarket.raw.op("->>")("event_ticker"),
            CoreMarket.raw.op("->>")("eventId"),
        ),
        group_item,
        CoreMarket.last_price,
        CoreMarket.last_volume_24h,
        CoreMarket.closes_at,
        CoreMarket.url,
        CoreMarket.raw.op("->>")("rules_primary"),
        func.coalesce(
            CoreMarket.raw.op("->>")("occurrence_datetime"),
            CoreMarket.raw.op("->>")("gameStartTime"),
        ),
        CoreMarket.raw.op("->>")("yes_sub_title"),
        CoreMarket.resolution,
        CoreMarket.resolved_at,
    ).where(
        CoreMarket.id.in_(candidate_ids),
        CoreMarket.resolved_at.is_not(None),
        CoreMarket.resolved_at >= now - timedelta(hours=hours),
        # Same winner-pattern gate as before, now over a PK-restricted set.
        or_(*winner_market_branches(ALL_GAMES_ALT)),
    )
    raw_rows = (await session.execute(stmt)).all()
    rows: list[em.Mkt] = []
    res_of: dict[int, tuple[str | None, object]] = {}
    for r in raw_rows:
        (mid, venue, title, external_id, event_key, grp, price, vol,
         closes_at, url, rules, match_raw, yes_sub, resolution, resolved_at) = r
        res_of[mid] = (str(resolution).lower() if resolution is not None else None, resolved_at)
        rows.append(em.Mkt(
            market_id=mid, venue=venue, title=title, external_id=external_id,
            event_key=event_key, group_item=grp,
            price=float(price) if price is not None else None,
            volume_24h=float(vol) if vol is not None else None,
            closes_at=closes_at, url=url, rules=rules,
            match_time=_parse_iso(match_raw), yes_sub=yes_sub,
        ))
    if not rows:
        return []

    def _leg_team(m: em.Mkt) -> str | None:
        if m.venue == "kalshi" and m.yes_sub:
            return m.yes_sub.strip()
        mt = em._KALSHI_WINNER.match(m.title.strip())
        return mt["winner"] if mt else None

    by_id = {m.market_id: m for m in rows}
    out: list[dict] = []
    for match in em.build_matches(rows):
        yes_teams: set[str] = set()
        poly_yes: bool | None = None
        settled_at = None
        for mid in match.get("market_ids") or []:
            m = by_id.get(mid)
            res, r_at = res_of.get(mid, (None, None))
            if r_at is not None and (settled_at is None or r_at > settled_at):
                settled_at = r_at
            if m is None or res not in ("yes", "no"):
                continue
            if m.venue == "polymarket":
                poly_yes = res == "yes"
                continue
            team = _leg_team(m)
            if team and res == "yes":
                yes_teams.add(em._team_key(em._strip_year(team)))
        winner = None
        keys = {em._team_key(match["team_a"]): match["team_a"], em._team_key(match["team_b"]): match["team_b"]}
        if len(yes_teams) == 1:
            winner = keys.get(next(iter(yes_teams)))
        elif poly_yes is not None:
            winner = match["team_a"] if poly_yes else match["team_b"]
        if winner is None and match.get("prob_a") is not None:
            # pinned-price fallback — a decided book reads ~0/100
            if match["prob_a"] >= 98:
                winner = match["team_a"]
            elif match["prob_a"] <= 2:
                winner = match["team_b"]
        out.append({
            **match,
            "settled": True,
            "winner": winner,
            "settled_at": settled_at.isoformat() if settled_at is not None else None,
        })
    out.sort(key=lambda m: m.get("settled_at") or "", reverse=True)
    return out[:12]


# Mirror of build_matches' own cross-venue clustering tolerance: the same match
# is logged with closes_at up to ~2 days apart across venues, so fixture
# identity is (game, team pair) + times within this window — a genuine rematch
# lands a full weekend later.
_TWIN_WINDOW = timedelta(hours=60)


def _settled_twin(match: dict, settled: list[dict]) -> bool:
    """True when `match` is the same fixture as a recently-settled entry.

    A partially-resolved fixture otherwise shows twice: its resolved legs put
    it in `settled`, while a leg still open on another venue — its book frozen
    at ~0/100 — keeps it in the live list (2026-09-14: Bushido vs ENJOY sat in
    both, "upcoming" at 100%/0%). Keys, not slugs or display names, are the
    identity (venue folds can pick different display names for one team); a
    side with no start time still counts as the twin — inside the 72h settled
    window that is the duplicate, not a dated rematch."""
    pair = frozenset((em._team_key(match["team_a"]), em._team_key(match["team_b"])))
    t = _parse_iso(match.get("starts_at"))
    for s in settled:
        if s["game"] != match["game"]:
            continue
        if frozenset((em._team_key(s["team_a"]), em._team_key(s["team_b"]))) != pair:
            continue
        st = _parse_iso(s.get("starts_at"))
        if t is None or st is None or abs(t - st) <= _TWIN_WINDOW:
            return True
    return False


async def compute_esports(
    session: AsyncSession, *, max_age_h: int = 168, trend_days: int = 14
) -> tuple[dict, dict[str, dict]]:
    """(board_payload, {slug: detail_payload}) — the whole esports vertical
    pre-computed once. `board_payload` feeds the list page; each detail feeds a
    match page (match + holdings + trend). Cutoff is generous (max_age_h) so the
    cache holds every recent match; the route trims to its own window in-memory."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=max_age_h)
    rows = await _winner_candidates(session, ALL_GAMES_ALT, cutoff)
    matches = em.build_matches(rows)
    settled = await _settled_matches(session)
    board = {
        "generated_at": now.isoformat(),
        # A settled fixture must not also sit in the live list (`_settled_twin`);
        # details below still cover the full fold so a deep-linked match page
        # keeps serving after its fixture settles.
        "matches": [m for m in matches if not _settled_twin(m, settled)],
        # Recently settled (≤72h) with the winner where derivable (D14) — kept
        # on the board so finished matches don't just vanish from the page.
        "settled": settled,
    }
    details: dict[str, dict] = {}
    for match in matches:
        details[match["slug"]] = {
            "generated_at": now.isoformat(),
            "match": match,
            "holdings": await _match_holdings(session, match, cutoff),
            "trend": await _match_trend(session, match, rows, now, trend_days),
        }
    return board, details
