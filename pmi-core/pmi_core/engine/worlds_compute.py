"""worlds_compute — pre-compute the LoL Worlds tournament board + per-match detail
from the markets.

The board is two layers (see tournaments/lol-worlds-2026.yaml): a STRUCTURE layer
(the published 3-stage format + seeding, loaded from the YAML, which never
disappears) and a DYNAMIC layer folded from the markets at compute time — live
odds from open winner markets, results from resolved ones. A finished series'
contract clears from the odds, but its slot + winner persist (from the structure
+ the resolution), so the tree/standings stay complete.

Reuses the esports cross-venue fold (`engine.esports_matches`) and its per-match
detail builders (`engine.esports_compute`): one match folds its Kalshi +
Polymarket + Robinhood winner legs into a single P(team_a wins), and each match
gets the same holdings + L2 win-% trend the esports match page shows. We fetch ALL
this tournament's matches (open AND resolved) once — Kalshi via an index-scoped
ticker range, Polymarket/Robinhood via their live winner markets — fold them, then
filter to this tournament (its name is in the Kalshi rules / Polymarket title tail,
not the title) and map each match onto a stage by its teams.

The markets price only the SERIES winner (who wins the BO3/BO5), not per-game, so
the detail is series win % over time + the backing contracts — no per-map odds.

No LLM, no score — market-implied odds + deterministic results only.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import resources

import yaml
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.engine import esports_compute as ec
from pmi_core.engine import esports_matches as em
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.engine.markets_board import market_url
from pmi_core.models import CoreMarket


def load_tournament(tid: str = "lol-worlds-2026") -> dict:
    """The tournament's structure YAML (published format + seeding)."""
    text = resources.files("pmi_core.tournaments").joinpath(f"{tid}.yaml").read_text()
    return yaml.safe_load(text)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Columns for the esports Mkt fold (same order _to_mkt unpacks).
_MKT_COLS = (
    CoreMarket.id,
    CoreMarket.venue,
    CoreMarket.title,
    CoreMarket.external_id,
    CoreMarket.raw.op("->>")("event_ticker"),
    CoreMarket.raw.op("->>")("groupItemTitle"),
    CoreMarket.last_price,
    CoreMarket.last_volume_24h,
    CoreMarket.closes_at,
    CoreMarket.url,
    CoreMarket.raw.op("->>")("rules_primary"),
    CoreMarket.raw.op("->>")("occurrence_datetime"),
    CoreMarket.raw.op("->>")("yes_sub_title"),
)


def _to_mkt(row: tuple) -> em.Mkt:
    (mid, venue, title, ext, event_key, grp, price, vol, closes, url, rules, mt, yes_sub) = row
    return em.Mkt(
        market_id=mid, venue=venue, title=title, external_id=ext, event_key=event_key,
        group_item=grp, price=float(price) if price is not None else None,
        volume_24h=float(vol) if vol is not None else None, closes_at=closes, url=url,
        rules=rules, match_time=_parse_iso(mt), yes_sub=yes_sub,
    )


def _lol_winner_poly_rh() -> list:
    """Polymarket / Robinhood winner-market shapes (Kalshi comes in via the ticker
    range, not this OR). Polymarket is narrowed to LoL by title; Robinhood's LoL
    titles ('Will X win the A vs. B?') carry NO game token, so this matches any
    Robinhood head-to-head and _normalize_robinhood() later keeps only the ones
    naming two Worlds-roster teams (and labels their game)."""
    grp = CoreMarket.raw.op("->>")("groupItemTitle")
    return [
        and_(
            CoreMarket.venue == "robinhood",
            CoreMarket.title.op("~*")(r"^Will .+ win the .+ vs\.? .+\??$"),
        ),
        and_(
            CoreMarket.venue == "polymarket",
            grp == "Match Winner",
            CoreMarket.title.op("~*")(r"^(League of Legends|LoL): .+ vs .+ \(BO[0-9]+\)"),
        ),
    ]


def _normalize_robinhood(rows: list[em.Mkt], roster: set[str]) -> list[em.Mkt]:
    """Robinhood LoL titles lack the '<game> match' suffix the fold's winner regex
    needs to parse + group them. For rows naming two roster teams, append it so
    build_matches reads them (game=lol) and folds them with the Kalshi/Polymarket
    legs; drop other Robinhood rows — without a game token we can't tell CS2 from
    LoL, and only roster matches are this tournament's anyway."""
    out: list[em.Mkt] = []
    for m in rows:
        if m.venue != "robinhood":
            out.append(m)
            continue
        title = m.title or ""
        if len({n for n in roster if n.lower() in title.lower()}) < 2:
            continue
        out.append(dataclasses.replace(m, title=f"{title.rstrip().rstrip('?').rstrip()} League of Legends match?"))
    return out


async def _manifold_holdings(
    session: AsyncSession, team_a: str, team_b: str, since: datetime
) -> list[dict]:
    """Manifold's Worlds markets naming both teams — shown in the holdings list as
    play-money (so they never enter the headline fold / weighting, per the display
    rule). Manifold uses short team names ('FURIA' not 'FURIA Esports'), so match on
    the first token of each name, scoped to Worlds titles."""
    sa, sb = team_a.split()[0], team_b.split()[0]
    stmt = select(
        CoreMarket.id, CoreMarket.venue, CoreMarket.title,
        CoreMarket.last_price, CoreMarket.last_volume_24h, CoreMarket.url,
        CoreMarket.external_id, CoreMarket.raw,
    ).where(
        CoreMarket.venue == "manifold",
        CoreMarket.last_price.is_not(None),
        CoreMarket.last_price_at >= since,
        CoreMarket.title.ilike("%worlds%"),
        CoreMarket.title.ilike(f"%{sa}%"),
        CoreMarket.title.ilike(f"%{sb}%"),
    )
    return [
        {
            "market_id": mid, "title": title, "venue": venue,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol) if vol is not None else None,
            "url": market_url(venue, external_id, raw, url),
            "category": "Play money", "side": None,
        }
        for mid, venue, title, price, vol, url, external_id, raw in (
            await session.execute(stmt)
        ).all()
    ]


def _ticker_month_prefixes(since: datetime, until: datetime) -> list[str]:
    """Kalshi ticker month stamps (``26OCT``) spanning [since, until]. The ticker
    date IS the match date, so a ``KXLOLGAME-26OCT`` ticker bound pulls only that
    month's matches — scoping the scan to the tournament window instead of every
    LoL match Kalshi ever listed."""
    mons = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    out, y, m = [], since.year, since.month
    while (y, m) <= (until.year, until.month):
        out.append(f"{y % 100:02d}{mons[m - 1]}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_ticker_range(mp: str):
    """A ``[lo, hi)`` external_id range for one ``KXLOLGAME-{mp}`` month. A manual
    range on (venue, external_id) uses the composite btree; ``LIKE 'prefix%'`` does
    NOT under the DB's non-C collation (Postgres can't fold it to a range), so it
    falls back to scanning every kalshi row — the resolved-scan hang. The upper
    bound increments the month stamp's last char (``26OCT`` → ``26OCU``), which is
    > any ``26OCT…`` ticker and < the next month's, since the stamps are ASCII."""
    lo = f"KXLOLGAME-{mp}"
    hi = f"KXLOLGAME-{mp[:-1]}{chr(ord(mp[-1]) + 1)}"
    return and_(CoreMarket.external_id >= lo, CoreMarket.external_id < hi)


async def _live_rows(session: AsyncSession, since: datetime) -> list[em.Mkt]:
    """Live cross-venue LoL winner rows (Kalshi + Polymarket + Robinhood) for the
    open matches — folds into the live odds. Same shape esports uses."""
    kalshi_live = and_(CoreMarket.venue == "kalshi", CoreMarket.external_id.like("KXLOLGAME-%"))
    stmt = select(*_MKT_COLS).where(
        *live_market_conditions(),
        CoreMarket.last_price.is_not(None),
        CoreMarket.last_price_at >= since,
        or_(kalshi_live, *_lol_winner_poly_rh()),
    )
    return [_to_mkt(r) for r in (await session.execute(stmt)).all()]


async def _resolved_rows(
    session: AsyncSession, since: datetime
) -> tuple[list[em.Mkt], dict[int, tuple[str | None, datetime | None, str | None]]]:
    """Decided Kalshi LoL winner rows (the authoritative result source), via the
    index-scoped ticker range, plus a ``market_id → (resolution, resolved_at,
    yes_sub_title)`` map. Folded SEPARATELY from the live rows: a double-elim
    rematch of the same pair (e.g. MVK vs FURIA twice, 48h apart) would otherwise
    land in build_matches' 60h cluster and the finished game would corrupt the open
    one's odds."""
    months = _ticker_month_prefixes(since, datetime.now(UTC))
    stmt = select(*_MKT_COLS, CoreMarket.resolution, CoreMarket.resolved_at).where(
        CoreMarket.venue == "kalshi",
        or_(*[_month_ticker_range(mp) for mp in months]),
        CoreMarket.resolved_at.is_not(None),
        CoreMarket.resolution.in_(("YES", "NO")),
    )
    rows: list[em.Mkt] = []
    res_map: dict[int, tuple[str | None, datetime | None, str | None]] = {}
    for row in (await session.execute(stmt)).all():
        mkt = _to_mkt(row[: len(_MKT_COLS)])
        rows.append(mkt)
        res_map[mkt.market_id] = (row[-2], row[-1], mkt.yes_sub)
    return rows, res_map


# How far a venue's listed start may sit from Riot's and still be the same match:
# a reshuffle moves a slot by a day or so; Swiss has no rematches and a knockout
# rematch is a stage (≥ a week) later.
_SCHEDULE_TOLERANCE = timedelta(hours=72)


def _official_start_lookup(
    schedule: list[dict], code_to_name: dict[str, str]
) -> Callable[[str, str, datetime | None], datetime | None]:
    """``(team_key_a, team_key_b, venue_start) → Riot's start`` from the YAML
    ``schedule``: the pair's published start nearest the venue's, within
    tolerance; None when the pair is unscheduled or the venue start is unknown."""
    official: dict[frozenset[str], list[datetime]] = {}
    for e in schedule:
        t = _parse_iso(e.get("starts_at"))
        if t is None:
            continue
        pair = frozenset(em._team_key(code_to_name.get(c, c)) for c in (e["a"], e["b"]))
        official.setdefault(pair, []).append(t)

    def lookup(key_a: str, key_b: str, venue_t: datetime | None) -> datetime | None:
        times = official.get(frozenset((key_a, key_b)))
        if not times or venue_t is None:
            return None
        best = min(times, key=lambda t: abs(t - venue_t))
        return best if abs(best - venue_t) <= _SCHEDULE_TOLERANCE else None

    return lookup


def _apply_official_starts(
    matches: list[dict], official_start: Callable[[str, str, datetime | None], datetime | None]
) -> None:
    """Overwrite each open match's ``starts_at`` with Riot's published start when
    the schedule has one. In place: the detail payloads hold these same dicts, so
    match pages get it too. The slug keeps the venue date — it is the match page's
    URL and is already linked."""
    for m in matches:
        best = official_start(
            em._team_key(m["team_a"]), em._team_key(m["team_b"]), _parse_iso(m.get("starts_at"))
        )
        if best is not None:
            m["starts_at"] = best.isoformat()


def _in_tournament(text: str | None, tokens: list[str]) -> bool:
    t = (text or "").lower()
    return any(tok.lower() in t for tok in tokens)


def _team_names(tournament: dict) -> dict[str, str]:
    """Code → canonical name, and each name keyed to itself, for mapping markets."""
    return {t["code"]: t["name"] for t in tournament.get("teams", [])}


def _resolve_match(
    match: dict, res_map: dict[int, tuple[str | None, datetime | None, str | None]]
) -> tuple[str | None, datetime | None]:
    """A match is resolved when one of its Kalshi legs has a YES/NO resolution.
    Returns (winner_name, resolved_at); (None, None) if still open. The YES leg's
    yes_sub_title names the winner."""
    winner: str | None = None
    resolved_at: datetime | None = None
    for mid in match.get("market_ids") or []:
        hit = res_map.get(mid)
        if not hit:
            continue
        resolution, rat, team = hit
        resolved_at = rat or resolved_at
        if resolution == "YES":
            winner = team
    return winner, resolved_at


async def _holdings_by_ids(session: AsyncSession, ids: list[int]) -> list[dict]:
    """The backing contracts for a match, by the fold's own market ids — works for
    open AND resolved legs (no live filter), unlike esports' name+time holdings
    query. Worlds has only winner legs (no per-map markets), so the fold ids are
    the full holdings set."""
    if not ids:
        return []
    stmt = select(
        CoreMarket.id, CoreMarket.venue, CoreMarket.title,
        CoreMarket.raw.op("->>")("groupItemTitle"),
        CoreMarket.last_price, CoreMarket.last_volume_24h, CoreMarket.url,
        CoreMarket.external_id, CoreMarket.raw, CoreMarket.raw.op("->>")("yes_sub_title"),
    ).where(CoreMarket.id.in_(ids))
    holdings = [
        {
            "market_id": mid,
            "title": title,
            "venue": venue,
            "price": float(price) if price is not None else None,
            "volume_24h": float(vol) if vol is not None else None,
            "url": market_url(venue, external_id, raw, url),
            "category": ec._holding_category(venue, title, grp),
            "side": ec._payout_side(venue, title, yes_sub),
        }
        for mid, venue, title, grp, price, vol, url, external_id, raw, yes_sub in (
            await session.execute(stmt)
        ).all()
    ]
    holdings.sort(key=lambda h: -(h["volume_24h"] or 0.0))
    return holdings


def _blank_untraded(holdings: list[dict]) -> list[dict]:
    """A Kalshi contract with no last trade (price 0/None) reads as 0% today, which
    looks like 'no chance' when it's really 'no trade yet' — Kalshi itself shows an
    order-book 'Chance' (e.g. a correct-score Yes 24¢ → ~13%). We can't show that
    mid: the yes_bid/ask we store for these low-volume markets is stale (full-width
    0.01/0.99) — the live book isn't in our pipeline. So show '—' (price None),
    honest 'no trades', rather than a fake 0% or a mid off stale quotes. See the
    freshness follow-up."""
    for h in holdings:
        if h["venue"] == "kalshi" and not h["price"]:
            h["price"] = None
    return holdings


def _resolve_bracket(
    stage: dict, live: list[dict], results: list[dict], code_to_name: dict[str, str]
) -> dict | None:
    """Fill the stage's bracket DAG from the folded markets. Each slot's two teams
    come from seed codes or an earlier slot's winner/loser; once both are known we
    match the pair to a resolved result (→ winner) or an open match (→ live odds).
    Returns per-slot teams/odds/winner/status + who advanced / was eliminated, for
    the bracket view. None if the stage has no bracket."""
    spec = stage.get("bracket")
    if not spec:
        return None

    def norm(n: str | None) -> str:
        return (n or "").strip().lower()

    # A team pair can play TWICE in double-elim (a rematch in the losers bracket),
    # so a single pair→market map is ambiguous. Keep every same-pair match sorted by
    # date and consume them in bracket order: the earliest market fills the earliest
    # slot (WB round 1), the next fills the rematch (losers final).
    from collections import defaultdict

    pool: dict[frozenset, list[dict]] = defaultdict(list)
    for r in results:
        pool[frozenset(norm(t) for t in r["teams"])].append({
            "status": "resolved", "date": r.get("resolved_at") or "",
            "winner": r["winner"], "slug": r.get("slug"), "prob_a": None,
            "team_a": r["team_a"],
        })
    for m in live:
        pool[frozenset({norm(m["team_a"]), norm(m["team_b"])})].append({
            "status": "live", "date": m.get("starts_at") or "",
            "winner": None, "slug": m.get("slug"), "prob_a": m["prob_a"],
            "team_a": m["team_a"],
        })
    for lst in pool.values():
        lst.sort(key=lambda e: e["date"])
    consumed: dict[frozenset, int] = defaultdict(int)

    slots: dict[str, dict] = {}
    eliminated: set[str] = set()

    def ref_team(ref: dict) -> str | None:
        if "seed" in ref:
            return code_to_name.get(ref["seed"], ref["seed"])
        src = slots.get(ref.get("winner") or ref.get("loser") or "")
        if not src or src["status"] != "resolved" or not src["winner"]:
            return None
        if "winner" in ref:
            return src["winner"]
        return src["team_a"] if norm(src["team_b"]) == norm(src["winner"]) else src["team_b"]

    out_matches: list[dict] = []
    for slot in spec:
        a, b = ref_team(slot["a"]), ref_team(slot["b"])
        s = {
            "id": slot["id"], "round": slot["round"], "group": slot["group"],
            "advances": bool(slot.get("advances")),
            "team_a": a, "team_b": b, "prob_a": None, "winner": None,
            "slug": None, "status": "pending",
        }
        if a and b:
            key = frozenset({norm(a), norm(b)})
            lst, idx = pool.get(key, []), consumed[frozenset({norm(a), norm(b)})]
            if idx < len(lst):
                e = lst[idx]
                consumed[key] += 1
                s["slug"] = e["slug"]
                s["status"] = e["status"]
                if e["status"] == "resolved":
                    s["winner"] = e["winner"]
                    s["prob_a"] = 100.0 if norm(e["winner"]) == norm(a) else 0.0
                elif e["prob_a"] is not None:
                    s["prob_a"] = e["prob_a"] if norm(e["team_a"]) == norm(a) else 100.0 - e["prob_a"]
        slots[slot["id"]] = s
        out_matches.append(s)
        # A loss in the losers bracket (or the qualification decider) is elimination;
        # a winners-bracket loss only drops you down, so it isn't counted here.
        if s["status"] == "resolved" and slot["group"] in ("losers", "final"):
            loser = s["team_a"] if norm(s["winner"]) == norm(s["team_b"]) else s["team_b"]
            if loser:
                eliminated.add(loser)

    qual = next((s for s in out_matches if s["advances"]), None)
    advanced = qual["winner"] if qual and qual["status"] == "resolved" else None
    return {
        "kind": stage.get("kind"),
        "advances": stage.get("advances"),
        "advanced": advanced,
        "eliminated": sorted(eliminated),
        "matches": out_matches,
    }


def _resolve_swiss(
    stage: dict, live: list[dict], results: list[dict], code_to_name: dict[str, str]
) -> dict | None:
    """Swiss standings + record-flow columns from the folded markets. Each team's
    W–L record is replayed from the resolved results in date order; a match sits in
    the record bracket its two (same-record) teams shared going in (0-0 → 1-0 / 0-1
    → …). First to ``advance_wins`` advances, ``eliminate_losses`` is out. Round 1 is
    the published draw (shown before a market exists); rounds 2–5 derive from results.
    None for a stage with no roster yet."""
    codes = stage.get("teams") or []
    if not codes:
        return None

    def norm(n: str | None) -> str:
        return (n or "").strip().lower()

    name_of = {c: code_to_name.get(c, c) for c in codes}
    name_by_norm = {norm(n): n for n in name_of.values()}
    abbr_by_norm = {norm(name_of[c]): c for c in codes}
    roster = set(name_by_norm)
    adv_w = stage.get("advance_wins", 3)
    elim_l = stage.get("eliminate_losses", 3)

    rec: dict[str, list[int]] = {n: [0, 0] for n in roster}  # norm name -> [wins, losses]
    by_bracket: dict[str, list[dict]] = {}

    def add(brk: str, m: dict) -> None:
        by_bracket.setdefault(brk, []).append(m)

    def in_roster(a: str, b: str) -> bool:
        return norm(a) in roster and norm(b) in roster

    # Resolved Swiss matches, chronological — replay to build records + bracket.
    res = sorted(
        (r for r in results if in_roster(r["team_a"], r["team_b"]) and r.get("winner")),
        key=lambda r: r.get("resolved_at") or "",
    )
    for r in res:
        a, b = norm(r["team_a"]), norm(r["team_b"])
        brk = f"{min(rec[a][0], rec[b][0])}-{min(rec[a][1], rec[b][1])}"
        w = norm(r["winner"])
        loser = b if w == a else a
        rec[w][0] += 1
        rec[loser][1] += 1
        add(brk, {
            "team_a": name_by_norm[a], "team_b": name_by_norm[b],
            "winner": name_by_norm.get(w), "prob_a": None,
            "status": "resolved", "slug": r.get("slug"), "starts_at": None,
        })

    seen = {frozenset({norm(m["team_a"]), norm(m["team_b"])}) for ms in by_bracket.values() for m in ms}

    # Open matches: bracket = the two teams' shared record. A valid Swiss pairing
    # always has both teams at the same record going in; when they differ it is a
    # premature pairing (the earlier round has not settled upstream yet), so hide
    # it rather than mis-bucketing it into the lower record column by min().
    for m in live:
        a, b = norm(m["team_a"]), norm(m["team_b"])
        if not in_roster(m["team_a"], m["team_b"]) or frozenset({a, b}) in seen:
            continue
        if rec[a] != rec[b]:
            continue
        seen.add(frozenset({a, b}))
        brk = f"{rec[a][0]}-{rec[a][1]}"
        add(brk, {
            "team_a": name_by_norm[a], "team_b": name_by_norm[b],
            "winner": None, "prob_a": m.get("prob_a"),
            "status": "live", "slug": m.get("slug"), "starts_at": m.get("starts_at"),
        })

    # Round 1 is pre-seeded: show the published pairing even before a market opens.
    for p in stage.get("round1", []):
        na, nb = name_of.get(p["a"], p["a"]), name_of.get(p["b"], p["b"])
        if frozenset({norm(na), norm(nb)}) in seen:
            continue
        add("0-0", {
            "team_a": na, "team_b": nb, "winner": None, "prob_a": None,
            "status": "pending", "slug": None, "starts_at": None,
        })

    def team_status(n: str) -> str:
        if rec[n][0] >= adv_w:
            return "advanced"
        if rec[n][1] >= elim_l:
            return "eliminated"
        return "alive"

    standings = sorted(
        (
            {
                "team": name_by_norm[n], "abbr": abbr_by_norm[n],
                "wins": rec[n][0], "losses": rec[n][1], "status": team_status(n),
            }
            for n in roster
        ),
        key=lambda s: (-s["wins"], s["losses"], s["team"]),
    )

    # Record brackets in flow order (3-win / 3-loss Swiss has exactly these nine
    # match-bearing states); a bracket where one more win hits advance_wins or one
    # more loss hits eliminate_losses is a BO3 decider, the rest BO1.
    order = ["0-0", "1-0", "0-1", "2-0", "1-1", "0-2", "2-1", "1-2", "2-2"]
    columns = [
        {
            "record": k,
            "format": "BO3" if (int(k[0]) + 1 >= adv_w or int(k[2]) + 1 >= elim_l) else "BO1",
            "matches": by_bracket[k],
        }
        for k in order
        if k in by_bracket
    ]
    return {
        "rounds": stage.get("rounds"),
        "advances": stage.get("advances"),
        "advance_wins": adv_w,
        "eliminate_losses": elim_l,
        "standings": standings,
        "columns": columns,
    }


async def compute_worlds(
    session: AsyncSession, tid: str = "lol-worlds-2026", *, trend_days: int = 14
) -> tuple[dict, dict[str, dict]]:
    """(board_payload, {slug: detail_payload}). The board is the structure from
    YAML with odds + results folded from the markets, assembled per stage; each
    detail feeds a match page (match + holdings + win-% trend), keyed by slug."""
    tour = load_tournament(tid)
    tokens = tour.get("tournament_tokens", [])
    now = datetime.now(UTC)
    code_to_name = _team_names(tour)
    roster = set(code_to_name.values())

    since_live = now - timedelta(days=3)
    live_rows = _normalize_robinhood(await _live_rows(session, since_live), roster)
    resolved_rows, res_map = await _resolved_rows(session, now - timedelta(days=45))
    # Riot's schedule keys the live fold too: a slot reshuffled after listing puts
    # one fixture's Kalshi and Polymarket starts a day+ apart, which would split it
    # into two matches (BLG–TES, 2026-10-28).
    official_start = _official_start_lookup(tour.get("schedule") or [], code_to_name)
    live = [
        m
        for m in em.build_matches(live_rows, official_start)
        if _in_tournament(m.get("tournament"), tokens)
    ]
    resolved_matches = [
        m for m in em.build_matches(resolved_rows) if _in_tournament(m.get("tournament"), tokens)
    ]

    details: dict[str, dict] = {}
    for m in live:
        # Esports' name+time holdings query (not just the fold ids) so the detail
        # surfaces EVERY real-money backing contract — the Kalshi series legs AND
        # correct-score (KXLOLSCORE 3-0/3-1/3-2), per-game, totals, props, plus
        # Robinhood's winner markets. Manifold is appended as play-money (shown, not
        # weighted), deduped by market_id.
        holdings = await ec._match_holdings(session, m, since_live)
        seen = {h["market_id"] for h in holdings}
        holdings += [
            h for h in await _manifold_holdings(session, m["team_a"], m["team_b"], since_live)
            if h["market_id"] not in seen
        ]
        holdings = _blank_untraded(holdings)
        details[m["slug"]] = {
            "generated_at": now.isoformat(),
            "match": m,
            "holdings": holdings,
            "trend": await ec._match_trend(session, m, live_rows, now, trend_days),
            "winner": None,
            "resolved_at": None,
        }

    # After the holdings queries: they window on the venue's start (Kalshi legs
    # close ~2 days after the ticker time), so Riot's start would drop them.
    _apply_official_starts(live, official_start)
    live.sort(key=lambda m: m.get("starts_at") or "~")

    results: list[dict] = []
    for m in resolved_matches:
        winner, resolved_at = _resolve_match(m, res_map)
        details[m["slug"]] = {
            "generated_at": now.isoformat(),
            "match": m,
            "holdings": _blank_untraded(
                await _holdings_by_ids(session, m.get("market_ids") or [])
            ),
            "trend": await ec._match_trend(session, m, resolved_rows, now, trend_days),
            "winner": winner,
            "resolved_at": resolved_at.isoformat() if resolved_at else None,
        }
        results.append({
            "teams": sorted([m["team_a"], m["team_b"]]),
            "team_a": m["team_a"], "team_b": m["team_b"],
            "winner": winner,
            "slug": m["slug"], "key": m["key"],
            "resolved_at": resolved_at.isoformat() if resolved_at else None,
        })

    stages_out = []
    for st in tour.get("stages", []):
        stage_codes = set(st.get("teams", []))
        stage_names = {code_to_name.get(c, c) for c in stage_codes}

        def _match_in_stage(pair: list[str], names: set[str] = stage_names) -> bool:
            # A match belongs to this stage when both its teams are on the stage's
            # roster. A stage with no roster yet (Swiss / Knockout before it seeds)
            # claims NOTHING — otherwise an empty stage would swallow every match.
            return bool(names) and all(any(n in p or p in n for n in names) for p in pair)

        st_live = [m for m in live if _match_in_stage([m["team_a"], m["team_b"]])]
        st_results = [r for r in results if _match_in_stage(r["teams"])]
        stages_out.append({
            "id": st["id"], "kind": st["kind"], "title": st["title"],
            "format": st.get("format"), "advances": st.get("advances"),
            "live_matches": st_live,
            "results": st_results,
            # A bracket DAG (e.g. Play-In double-elim) resolved from these matches —
            # the tree view. None for stages without one (Swiss uses `swiss`).
            "bracket": _resolve_bracket(st, st_live, st_results, code_to_name),
            # Swiss standings + record-flow columns; None for non-Swiss stages.
            "swiss": (
                _resolve_swiss(st, st_live, st_results, code_to_name)
                if st.get("kind") == "swiss"
                else None
            ),
        })

    board = {
        "id": tour["id"], "game": tour["game"], "title": tour["title"],
        "final": tour.get("final"), "generated_at": now.isoformat(),
        "stages": stages_out,
    }
    return board, details
