"""Pure helpers for GET /esports/matches — fold esports markets into cross-venue
matches (CS2 + Valorant).

No SQLAlchemy (``wc_matchups.py`` / ``markets_board.py`` precedent): the route
selects ``core_markets`` rows and hands tuples here, so parsing / odds math is
unit-testable in isolation and reads off last-price columns only.

The platform value is **combining exchanges on one question** (CORR-3.12; the
same thesis behind ``wc_matchups``' ``cross_source_edge``). So one match folds
its two venues:

  * **Kalshi** — two per-team winner markets ``Will {W} win the {A} vs. {B}
    {Game} match?`` sharing an ``event_ticker``; vig-stripped into P(A wins).
  * **Polymarket** — one ``groupItemTitle='Match Winner'`` market
    ``{Game}: {A} vs {B} (BOx) - {Tournament}`` whose ``last_price`` IS P(first-
    named team wins); it also carries the tournament name Kalshi omits.

Both venues use near-identical team names, so a match keys on
``(game, {normalized team pair})``; a small alias table covers the few
divergences. The combined reading is a volume-weighted P(A wins) across venues,
and ``edge`` is the cross-venue spread — the value/arb signal.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# Kalshi event tickers stamp the match time in US Eastern wall-clock (a US-exchange
# convention), e.g. KXLOLGAME-26OCT231300… = Oct 23, 1 PM ET (verified against the
# official Worlds 2026 schedule). Parse in ET and convert to real UTC — treating it
# as UTC left starts_at 4-5h early.
_TICKER_TZ = ZoneInfo("America/New_York")

_MIN_TIME = datetime.min.replace(tzinfo=UTC)

_GAME_SLUG: dict[str, str] = {
    "cs2": "cs2",
    "counter-strike": "cs2",
    "valorant": "valorant",
    # LoL: Kalshi titles say "League of Legends", Polymarket says "LoL:".
    "league of legends": "lol",
    "lol": "lol",
}
_GAME_ALT = "|".join(re.escape(g) for g in _GAME_SLUG)

_KALSHI_WINNER = re.compile(
    rf"^Will (?P<winner>.+?) win the (?P<a>.+?) vs\.? (?P<b>.+?) "
    rf"(?P<game>{_GAME_ALT}) match\??$",
    re.IGNORECASE,
)
# Polymarket "Match Winner": "{Game}: {A} vs {B} (BOx) - {Tournament}". last_price
# = P(A wins). The tournament tail is optional.
_POLY_MATCH = re.compile(
    rf"^(?P<game>{_GAME_ALT}): (?P<a>.+?) vs (?P<b>.+?) \(BO\d+\)"
    rf"(?: - (?P<tourney>.+))?$",
    re.IGNORECASE,
)
# Kalshi puts the tournament in the resolution rules, not the title:
# "If X wins the {Tournament}: {A} vs. {B} {game} match originally scheduled…".
_KALSHI_TOURNEY = re.compile(r"wins? the (.+?): .+ vs", re.IGNORECASE)

# Kalshi's NEW esports winner format (~2026-08): the title dropped the matchup
# ("Maryville University wins" instead of "Will X win the A vs B LoL match?"),
# so the game + teams no longer live in the title. Identify by the stable series
# prefix (KX{GAME}GAME) and read each leg's team from ``yes_sub_title``. Old
# format is gone on Kalshi but the grammar lives on for Robinhood (see
# ``_kalshi_readings``), so both paths stay.
_KALSHI_ESPORTS_TICKER = re.compile(r"^KX(LOL|CS2|VALORANT)GAME-", re.IGNORECASE)

# Kalshi esports tickers encode the REAL match datetime right after the series
# prefix: "KXLOLGAME-26OCT171400FURMVK-FUR" → 2026-10-17 14:00. This is the only
# reliable match-start signal — the `occurrence` JSON field is empty and `closes_at`
# is the match + ~2 days (a venue convention), which collides a rematch with the
# prior match's close. Used to separate same-pair matches (see `_members_match_start`).
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
)}
_TICKER_DT = re.compile(
    r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{2})(\d{2})(?=[A-Z])"
)


def _ticker_match_time(external_id: str | None) -> datetime | None:
    """Real match start parsed from a Kalshi esports ticker's embedded datetime, or
    None if the id carries none (Polymarket numeric ids, Robinhood UUIDs)."""
    if not external_id:
        return None
    m = _TICKER_DT.search(external_id)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    try:
        et = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=_TICKER_TZ)
        return et.astimezone(UTC)
    except ValueError:
        return None


def _kalshi_ticker_game(external_id: str | None) -> str | None:
    """Game slug from a Kalshi esports winner ticker prefix, else None."""
    if not external_id:
        return None
    m = _KALSHI_ESPORTS_TICKER.match(external_id)
    return m.group(1).lower() if m else None

# Known cross-venue team-name divergences (normalized-key → canonical key). Most
# names already match; this is the safety net for the few that don't.
_TEAM_ALIAS: dict[str, str] = {
    "navi": "natusvincere",
    "vp": "virtuspro",
    "faze": "fazeclan",
}


# Generic org-suffix noise one venue includes and the other drops ("Dragon
# Ranger Gaming" vs "Dragon Ranger"). Stripped before keying so the pair still
# matches. NOT stripped: tier markers (academy / challengers / prospects) — those
# distinguish a farm squad from the main roster, so "DN SOOPers Challengers" must
# key differently from "DN SOOPers" or two distinct matches fold into one.
_TEAM_SUFFIX = re.compile(
    r"\b(esports?|gaming|club|team|gg|gc)\b",
    re.IGNORECASE,
)


def _team_key(name: str) -> str:
    stripped = re.sub(r"[^a-z0-9]", "", _TEAM_SUFFIX.sub(" ", name.lower()))
    k = stripped or re.sub(r"[^a-z0-9]", "", name.lower())  # don't strip to empty
    return _TEAM_ALIAS.get(k, k)


def _strip_year(name: str) -> str:
    """Drop a leading season year Robinhood prefixes onto the first team in its
    "<winner> win the 2026 <A> vs <B>" titles — "2026 Astralis" → "Astralis" —
    so its key matches the same team on Kalshi/Polymarket. No-op elsewhere."""
    return re.sub(r"^\s*(?:19|20)\d{2}\s+", "", name).strip()


def _slugify(s: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "x"


def _match_slug(game: str, name_a: str, name_b: str, starts_at: datetime | None) -> str:
    """Readable, resolvable match id: 'cs2-noir-verse-vs-ex-rustec-aug18'. Two
    matches of the same pair on the same day collide (rare) — the route resolves
    a slug to the soonest such match."""
    parts = [game, _slugify(name_a), "vs", _slugify(name_b)]
    if starts_at:
        parts.append(starts_at.strftime("%b%d").lower())
    return "-".join(parts)


@dataclass(frozen=True)
class Mkt:
    """One candidate row — the route maps its SELECT onto this."""

    market_id: int
    venue: str
    title: str
    external_id: str | None    # kalshi ticker — its team-code suffix is the abbrev
    event_key: str | None      # kalshi raw->>'event_ticker'
    group_item: str | None     # polymarket raw->>'groupItemTitle'
    price: float | None        # last_price 0–1
    volume_24h: float | None
    closes_at: datetime | None
    url: str | None
    rules: str | None = None   # kalshi raw->>'rules_primary' — carries the tournament
    match_time: datetime | None = None  # real match start (occurrence / gameStartTime)
    yes_sub: str | None = None  # kalshi raw->>'yes_sub_title' — the leg's team (new format)


@dataclass
class _Reading:
    """One venue's read on a match, normalised so ``p_a`` = P(``name_a`` wins)."""

    venue: str
    game: str
    key_a: str
    key_b: str
    name_a: str
    name_b: str
    p_a: float | None
    volume: float
    market_ids: list[int]
    closes_at: datetime | None    # venue close (kalshi +2d) — display/sort fallback only
    match_time: datetime | None   # real match start — used for starts_at display/sort
    match_start: datetime | None  # best real match start (ticker-derived) — clustering key
    tournament: str | None
    abbr: dict[str, str]  # normalized team key → short code (Kalshi ticker; {} for poly)
    # Pre-vig-strip sum of the two YES legs (D14 book_sum — the WC precedent):
    # 1.043 = a 4.3% vig book. None when the venue gives one two-outcome market
    # (Polymarket — no leg pair to sum) or a leg's book is empty.
    book_sum: float | None = None


@dataclass
class MatchSource:
    venue: str
    p_a: float | None       # this venue's P(team_a wins), 0..100
    volume_24h: float
    book_sum: float | None = None  # pre-strip YES-leg sum (1.043 = 4.3% vig); None when N/A

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "p_a": self.p_a,
            "volume_24h": round(self.volume_24h, 2),
            "book_sum": round(self.book_sum, 4) if self.book_sum is not None else None,
        }


@dataclass
class Match:
    key: str
    slug: str                 # readable id, e.g. 'cs2-noir-verse-vs-ex-rustec-aug18'
    game: str
    team_a: str
    team_b: str
    team_a_abbr: str          # short code (Kalshi ticker) or the full name if none
    team_b_abbr: str
    prob_a: float | None      # combined, vol-weighted, 0..100
    prob_b: float | None
    favorite: str | None
    edge: float               # cross-venue spread in P(team_a), 0..100
    tournament: str | None
    starts_at: datetime | None
    volume_24h: float
    n_contracts: int
    venues: list[str]
    market_ids: list[int] = field(default_factory=list)
    sources: list[MatchSource] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "slug": self.slug,
            "game": self.game,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "team_a_abbr": self.team_a_abbr,
            "team_b_abbr": self.team_b_abbr,
            "prob_a": self.prob_a,
            "prob_b": self.prob_b,
            "favorite": self.favorite,
            "edge": round(self.edge, 1),
            "tournament": self.tournament,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "volume_24h": round(self.volume_24h, 2),
            "n_contracts": self.n_contracts,
            "venues": self.venues,
            "market_ids": self.market_ids,
            "sources": [s.as_dict() for s in self.sources],
        }


def _kalshi_readings(rows: list[Mkt], venue: str = "kalshi") -> list[_Reading]:
    """Group winner rows by event key, vig-strip each pair → readings.

    Robinhood uses the identical title grammar ("Will <team> win the <A> vs <B>
    <game> match?") and two complementary YES legs per match, so it reuses this
    parser via ``venue="robinhood"`` — its legs group on the coalesced eventId
    (see routes/esports.py) and ``_ticker_abbr`` no-ops on the UUID external_id,
    falling back to the dataset-wide short-code map.
    """
    groups: dict[str, list[Mkt]] = {}
    for m in rows:
        groups.setdefault(m.event_key or m.title, []).append(m)

    out: list[_Reading] = []
    for members in groups.values():
        # Old title grammar first (Robinhood + any legacy Kalshi row); the new
        # Kalshi title dropped the matchup, so fall back to the yes_sub_title path.
        r = _read_title_group(members, venue) or _read_kalshi_yes_sub_group(members, venue)
        if r is not None:
            out.append(r)
    return out


def _tourney_from_rules(members: list[Mkt]) -> str | None:
    return next(
        (mt.group(1).strip() for m in members
         if m.rules and (mt := _KALSHI_TOURNEY.search(m.rules))),
        None,
    )


def _read_title_group(members: list[Mkt], venue: str) -> _Reading | None:
    """Legacy grammar: teams/game parsed from ``Will W win the A vs B <game>
    match?`` titles. Still used by Robinhood (unchanged) and any old Kalshi row."""
    name_a = name_b = game = None
    price_by_winner: dict[str, float] = {}
    abbr_by_key: dict[str, str] = {}
    for m in members:
        mt = _KALSHI_WINNER.match(m.title.strip())
        if not mt or m.price is None:
            continue
        name_a = name_a or _strip_year(mt["a"])
        name_b = name_b or _strip_year(mt["b"])
        game = game or _GAME_SLUG.get(mt["game"].lower())
        price_by_winner[_team_key(_strip_year(mt["winner"]))] = m.price
        code = _ticker_abbr(m.external_id)  # this winner leg's team short code
        if code:
            abbr_by_key[_team_key(_strip_year(mt["winner"]))] = code
    if not (name_a and name_b and game):
        return None
    leg_a = price_by_winner.get(_team_key(name_a))
    leg_b = price_by_winner.get(_team_key(name_b))
    p_a = _vig_strip_a(leg_a, leg_b)
    if p_a is None:
        return None
    return _Reading(
        venue=venue,
        game=game,
        book_sum=_book_sum(leg_a, leg_b),
        key_a=_team_key(name_a),
        key_b=_team_key(name_b),
        name_a=name_a,
        name_b=name_b,
        p_a=p_a,
        volume=sum(m.volume_24h or 0.0 for m in members),
        market_ids=[m.market_id for m in members],
        closes_at=min((m.closes_at for m in members if m.closes_at), default=None),
        match_time=min((m.match_time for m in members if m.match_time), default=None),
        match_start=_members_match_start(members, venue),
        tournament=_tourney_from_rules(members),
        abbr=abbr_by_key,
    )


def _read_kalshi_yes_sub_group(members: list[Mkt], venue: str) -> _Reading | None:
    """New Kalshi format: each leg's team is its ``yes_sub_title``, the game is the
    ``KX{GAME}GAME`` ticker prefix, and the two legs share an event_ticker."""
    game = None
    teams: dict[str, list] = {}  # team_key -> [name, price, abbr]
    for m in members:
        if m.price is None or not m.yes_sub:
            continue
        g = _kalshi_ticker_game(m.external_id)
        if g is None:
            return None  # not a new-format Kalshi esports row
        game = game or g
        name = _strip_year(m.yes_sub.strip())
        teams.setdefault(_team_key(name), [name, m.price, _ticker_abbr(m.external_id)])
    if game is None or len(teams) != 2:
        return None
    (key_a, (name_a, pa, abbr_a)), (key_b, (name_b, pb, abbr_b)) = list(teams.items())
    p_a = _vig_strip_a(pa, pb)
    if p_a is None:
        return None
    abbr_by_key = {k: c for k, c in ((key_a, abbr_a), (key_b, abbr_b)) if c}
    return _Reading(
        venue=venue,
        game=game,
        book_sum=_book_sum(pa, pb),
        key_a=key_a,
        key_b=key_b,
        name_a=name_a,
        name_b=name_b,
        p_a=p_a,
        volume=sum(m.volume_24h or 0.0 for m in members),
        market_ids=[m.market_id for m in members],
        closes_at=min((m.closes_at for m in members if m.closes_at), default=None),
        match_time=min((m.match_time for m in members if m.match_time), default=None),
        match_start=_members_match_start(members, venue),
        tournament=_tourney_from_rules(members),
        abbr=abbr_by_key,
    )


def _ticker_abbr(external_id: str | None) -> str | None:
    """Kalshi winner ticker → the team's short code (its ``-{CODE}`` suffix).
    ``KXVALORANTGAME-26AUG112100NOSAEB-AEB`` → ``AEB``. Skip numeric map suffixes."""
    if not external_id or "-" not in external_id:
        return None
    code = external_id.rsplit("-", 1)[-1].strip()
    return code if code and not code.isdigit() and 1 < len(code) <= 8 else None


def _members_match_start(members: list[Mkt], venue: str) -> datetime | None:
    """Real match start for separating same-pair matches: the Kalshi ticker encodes
    it; else the venue's own match_time; else closes_at normalised (Kalshi/Robinhood
    close ~2 days after the match, Polymarket ~at match time)."""
    for m in members:
        tt = _ticker_match_time(m.external_id)
        if tt:
            return tt
    mt = min((m.match_time for m in members if m.match_time), default=None)
    if mt:
        return mt
    closes = min((m.closes_at for m in members if m.closes_at), default=None)
    if closes and venue in ("kalshi", "robinhood"):
        return closes - timedelta(days=2)
    return closes


def _poly_readings(rows: list[Mkt]) -> list[_Reading]:
    """Each Polymarket 'Match Winner' market is one reading (price = P(A))."""
    out: list[_Reading] = []
    for m in rows:
        if (m.group_item or "").strip().lower() != "match winner" or m.price is None:
            continue
        mt = _POLY_MATCH.match(m.title.strip())
        if not mt:
            continue
        game = _GAME_SLUG.get(mt["game"].lower())
        if not game:
            continue
        a, b = mt["a"].strip(), mt["b"].strip()
        out.append(
            _Reading(
                venue="polymarket",
                game=game,
                key_a=_team_key(a),
                key_b=_team_key(b),
                name_a=a,
                name_b=b,
                p_a=m.price,
                volume=m.volume_24h or 0.0,
                market_ids=[m.market_id],
                closes_at=m.closes_at,
                match_time=m.match_time,
                match_start=_members_match_start([m], "polymarket"),
                tournament=(mt["tourney"] or "").strip() or None,
                abbr={},  # Polymarket external_id is a numeric id — no team code
            )
        )
    return out


_EMPTY_BOOK = 0.005  # a YES leg at/below this is an un-opened book (no bids), not a real price


def _book_sum(p_a: float | None, p_b: float | None) -> float | None:
    """Pre-strip sum of the two YES legs — the venue's vig, shown as-is on the
    match page. Only meaningful when BOTH legs carry a real book."""
    a_book = p_a is not None and p_a > _EMPTY_BOOK
    b_book = p_b is not None and p_b > _EMPTY_BOOK
    return (p_a + p_b) if (a_book and b_book) else None  # type: ignore[operator]


def _vig_strip_a(p_a: float | None, p_b: float | None) -> float | None:
    """Two complementary YES prices → P(A wins) 0..1, vig removed.

    A leg priced ~0 is an EMPTY/un-opened book (Kalshi lists 0.000 before a match
    trades, or one side of a thin book has no bids), NOT a real 0% team. So:
      * both empty → no signal (None);
      * one empty → read the OTHER leg's price straight (an empty opposing leg must
        not vig-inflate the favourite to 100% — the bug behind PMI=100 on a book
        that really reads ~66%). A genuinely decided match still gives 100 because
        the winning leg itself is ~1.0.
      * both real → vig-strip.
    """
    a_book = p_a is not None and p_a > _EMPTY_BOOK
    b_book = p_b is not None and p_b > _EMPTY_BOOK
    if a_book and b_book:
        return p_a / (p_a + p_b)  # type: ignore[operator]
    if a_book:
        return p_a
    if b_book:
        return 1.0 - p_b  # type: ignore[operator]
    return None


def _cluster_by_time(
    readings: list[_Reading],
    thresh: timedelta,
    start: Callable[[_Reading], datetime | None] = lambda r: r.match_start,
) -> list[list[_Reading]]:
    """Split a team-pair's readings into distinct matches by MATCH-START proximity:
    reads within ``thresh`` chain into one cluster, a gap starts a new one. Untimed
    reads each stand alone. Keyed on ``match_start`` (real match time) not ``closes_at``
    — Kalshi/Robinhood close ~2d after the match, so on the close-time axis a rematch
    2 days later collides with the prior match's close and the two wrongly merge.
    ``start`` lets a caller key on a better start than the venue's (an official
    schedule) without changing what the folded match reports."""
    timed = [(t, r) for r in readings if (t := start(r)) is not None]
    clusters: list[list[_Reading]] = []
    last: datetime | None = None
    for t, r in sorted(timed, key=lambda x: x[0]):
        if clusters and last is not None and (t - last) <= thresh:
            clusters[-1].append(r)
        else:
            clusters.append([r])
        last = t
    clusters.extend([r] for r in readings if start(r) is None)
    return clusters


def _fold_cluster(members: list[_Reading], global_abbr: dict[str, str] | None = None) -> Match | None:
    """Combine one cluster's per-venue readings into a Match (vol-weighted P(A))."""
    first = members[0]
    key_a, key_b = first.key_a, first.key_b
    name_a, name_b = first.name_a, first.name_b
    # Short codes come from whichever member has them (Kalshi ticker); fall back to
    # the dataset-wide map (a Polymarket-only match still gets a code if that team
    # was seen on Kalshi elsewhere), then to the full name.
    abbrs: dict[str, str] = dict(global_abbr or {})
    for r in members:
        abbrs.update(r.abbr)
    # Collapse to ONE reading per venue (volume-weighted P(A)) before combining.
    # A venue can list two markets for the same pair inside the 60h window (a
    # re-listing, or a second map); folding those raw would let two Kalshi reads
    # manufacture a bogus cross-"exchange" edge. `edge` must be a spread across
    # DISTINCT venues, so aggregate within a venue first — the high-volume book
    # dominates, and a lone venue yields edge 0.
    by_venue: dict[str, list[tuple[float, float, float | None]]] = {}  # venue -> [(p_a, volume, book_sum)]
    for r in members:
        if r.p_a is None:
            continue
        p = r.p_a if r.key_a == key_a else (1.0 - r.p_a)  # align to canonical A
        by_venue.setdefault(r.venue, []).append((p, r.volume, r.book_sum))
    if not by_venue:
        return None

    srcs: list[MatchSource] = []
    venue_p: list[tuple[float, float]] = []  # (venue p_a, venue volume)
    for venue, pws in by_venue.items():
        vw = sum(w for _, w, _b in pws)
        vp = (
            sum(p * w for p, w, _b in pws) / vw
            if vw > 0
            else sum(p for p, _w, _b in pws) / len(pws)
        )
        # Venue book_sum: volume-weighted over the readings that have one
        # (a venue folding a real pair + an empty-leg re-listing keeps the
        # real pair's vig); None when none do.
        bws = [(b, w) for _p, w, b in pws if b is not None]
        bsum = (
            (sum(b * w for b, w in bws) / sum(w for _b, w in bws))
            if bws and sum(w for _b, w in bws) > 0
            else (bws[0][0] if bws else None)
        )
        srcs.append(MatchSource(venue=venue, p_a=round(vp * 100, 1), volume_24h=vw, book_sum=bsum))
        venue_p.append((vp, vw))

    wsum = sum(w for _, w in venue_p)
    combined = (
        sum(p * w for p, w in venue_p) / wsum
        if wsum > 0
        else sum(p for p, _ in venue_p) / len(venue_p)
    )
    prob_a = round(combined * 100, 1)
    edge = (max(p for p, _ in venue_p) - min(p for p, _ in venue_p)) * 100
    all_ids = sorted({mid for r in members for mid in r.market_ids})
    # Real match start (ticker-derived where possible); closes_at is a +2d venue
    # convention so it would date the match ~2 days late (and the slug with it).
    starts_at = min(
        (t for r in members if (t := r.match_start or r.match_time or r.closes_at)),
        default=None,
    )
    return Match(
        key=str(all_ids[0]) if all_ids else name_a,
        slug=_match_slug(first.game, name_a, name_b, starts_at),
        game=first.game,
        team_a=name_a,
        team_b=name_b,
        team_a_abbr=abbrs.get(key_a, name_a),
        team_b_abbr=abbrs.get(key_b, name_b),
        prob_a=prob_a,
        prob_b=round(100 - prob_a, 1),
        favorite=name_a if prob_a >= 50 else name_b,
        edge=edge,
        tournament=next((r.tournament for r in members if r.tournament), None),
        starts_at=starts_at,
        volume_24h=sum(r.volume for r in members),
        n_contracts=sum(len(r.market_ids) for r in members),
        venues=sorted({r.venue for r in members}),
        market_ids=all_ids,
        sources=sorted(srcs, key=lambda s: -s.volume_24h),
    )


def build_matches(
    rows: list[Mkt],
    official_start: Callable[[str, str, datetime | None], datetime | None] | None = None,
) -> list[dict]:
    """Fold Kalshi + Robinhood + Polymarket rows into cross-venue matches, soonest-first.

    ``official_start(key_a, key_b, venue_start)`` — an organiser's start for that
    pair near the venue's, or None. When given, venue reads of one fixture cluster
    on it, so a slot reshuffled after listing (venue times a day+ apart) still
    folds into one match. Display times and slugs still come from the venues."""
    def cluster_start(r: _Reading) -> datetime | None:
        if official_start is None:
            return r.match_start
        return official_start(r.key_a, r.key_b, r.match_start) or r.match_start

    readings = _kalshi_readings([r for r in rows if r.venue == "kalshi"])
    readings += _kalshi_readings([r for r in rows if r.venue == "robinhood"], venue="robinhood")
    readings += _poly_readings([r for r in rows if r.venue == "polymarket"])

    # Dataset-wide team-key → short-code map (Kalshi tickers carry codes; Polymarket
    # doesn't). Lets a Polymarket-only match borrow a code for a team that appears on
    # Kalshi in some other match, instead of falling back to the long full name.
    global_abbr: dict[str, str] = {}
    for r in readings:
        global_abbr.update(r.abbr)

    # Group by (game, team pair), then split each pair into distinct MATCHES by
    # clustering on real match_start: same-match reads (Kalshi ticker time ≈ Poly close)
    # fold; a rematch a day+ later is its own cluster.
    by_pair: dict[tuple[str, frozenset[str]], list[_Reading]] = {}
    for r in readings:
        by_pair.setdefault((r.game, frozenset((r.key_a, r.key_b))), []).append(r)

    # 18h: clustering is on real match_start (not closes_at), so a single match's
    # per-venue reads sit within a few hours (venue timezone + poly closing ~at match
    # end), while a rematch is ≥1 day later → its own cluster. The old 60h window was
    # a workaround for closes_at's +2d Kalshi convention, which match_start removes.
    out: list[Match] = []
    for pair_rows in by_pair.values():
        for cluster in _cluster_by_time(pair_rows, timedelta(hours=18), cluster_start):
            m = _fold_cluster(cluster, global_abbr)
            if m is None:
                continue
            if len(m.venues) > 1 and m.edge > 25:
                # Venues disagree far too much to be the same match — this is a
                # finished game on one venue clustered with an upcoming rematch on
                # the other (the 60h window's cost). Emit each venue on its own
                # rather than invent a 40-60pt "cross-exchange gap".
                for v in sorted({r.venue for r in cluster}):
                    sub = _fold_cluster([r for r in cluster if r.venue == v], global_abbr)
                    if sub is not None:
                        out.append(sub)
            else:
                out.append(m)

    out.sort(key=lambda m: (m.starts_at is None, m.starts_at or _MIN_TIME, -m.volume_24h))
    return [m.as_dict() for m in out]


def favourite_market(rows: list[Mkt], match: dict) -> tuple[int, bool] | None:
    """Pick the highest-volume Match Winner market for this match and report
    whether its ``last_price`` reads as P(favourite) (True) or P(the other team)
    (False) — so the L2 trend can orient a single contract's price history to the
    favourite's win %. Returns None if no winner market matches the pair."""
    ka, kb = _team_key(match["team_a"]), _team_key(match["team_b"])
    pair = {ka, kb}
    fav = _team_key(match["favorite"]) if match.get("favorite") else ka
    fold_ids = set(match.get("market_ids") or [])  # this match's own winner legs
    best: tuple[float, int, bool] | None = None  # (volume, market_id, price_is_fav)
    for m in rows:
        vol = m.volume_24h or 0.0
        if m.venue in ("kalshi", "robinhood"):
            mt = _KALSHI_WINNER.match(m.title.strip())  # robinhood shares the grammar
            if mt:  # old grammar (Robinhood + any legacy Kalshi title)
                if _GAME_SLUG.get(mt["game"].lower()) != match["game"]:
                    continue
                if {_team_key(_strip_year(mt["a"])), _team_key(_strip_year(mt["b"]))} != pair:
                    continue
                is_fav = _team_key(_strip_year(mt["winner"])) == fav  # price = P(winner)
            elif m.venue == "kalshi" and m.market_id in fold_ids and m.yes_sub:
                # New-format Kalshi: no matchup in the title, so trust the fold's
                # own market_ids and read the team from yes_sub. price = P(that team).
                is_fav = _team_key(_strip_year(m.yes_sub)) == fav
            else:
                continue
        elif m.venue == "polymarket":
            mt = _POLY_MATCH.match(m.title.strip())
            if not mt or _GAME_SLUG.get(mt["game"].lower()) != match["game"]:
                continue
            if {_team_key(mt["a"]), _team_key(mt["b"])} != pair:
                continue
            is_fav = _team_key(mt["a"]) == fav  # poly price = P(team A)
        else:
            continue
        if best is None or vol > best[0]:
            best = (vol, m.market_id, is_fav)
    return (best[1], best[2]) if best else None
