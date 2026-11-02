"""Pure helpers for GET /wc/matchups — WC-2026 matchup odds + sweet-rate flags.

Kept free of SQLAlchemy (markets_board.py precedent) so the fixture parsing /
odds math is unit-testable in isolation. The route in `routes/wc.py` fetches
the raw market rows and hands the tuples here.

Three market families feed the board (all read off `core_markets` last-price
columns — no LLM, no cache):

  * ladder   — polymarket "Will <team> reach the {Quarterfinals,Semifinals} at
               the 2026 FIFA World Cup?" + "Will <team> win the 2026 FIFA World
               Cup?" and gemini "Men's World Cup Semifinals Qualifiers? - <team>".
               Confederation rows ("Will Europe (UEFA) win …") are dropped.
  * direct   — polymarket "<A> vs. <B>: Team to Advance" (YES = first-named
               team advances; verified against the ladder: Argentina .86 ==
               reach-QF .86) and kalshi "<A> vs <B>: To Advance - <X> advances"
               (one leg per side; the pair is vig-stripped by normalisation).
  * fixtures — the match table itself is DERIVED from the direct markets: a
               pair (A, B) exists iff a direct market names two ladder teams.

Round inference is probabilistic, not hardcoded: a fixture whose two teams'
reach-QF prices sum to ~1 is a Round-of-16 tie (the winner takes the QF slot);
if instead the reach-SEMIFINAL prices sum to ~1 both teams are already in the
QF and the ladder's SF rung IS this matchup's price. This is the same
complementarity the bucket collapser exploits — no bracket table needed.

"Sweet rate" (甜蜜賠率): a matchup is flagged sweet when either
  * ``underdog_odds`` — one side's decimal odds ≥ ``sweet_odds`` (a high-payout
    underdog exists), or
  * ``cross_source_edge`` — the same outcome is priced ≥ ``min_edge`` apart
    across sources (cross-venue / ladder-vs-direct divergence: buy the cheap
    venue, the platform's CORR-3.12 cross-venue thesis in miniature).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Input row — the route maps its SELECT onto this.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mkt:
    market_id: int
    venue: str
    title: str
    price: float | None       # last_price 0–1, None = never priced
    volume_24h: float | None  # None = no volume data (distinct from 0)


# ---------------------------------------------------------------------------
# Ladder parsing
# ---------------------------------------------------------------------------

_PM_LADDER_RE = re.compile(
    r"^Will (?P<team>[^()?]+?) reach the (?P<rung>Quarterfinals|Semifinals) "
    r"at the 2026 FIFA World Cup\?",
    re.IGNORECASE,
)
_PM_WIN_RE = re.compile(
    r"^Will (?P<team>[^()?]+?) win the 2026 FIFA World Cup\?", re.IGNORECASE
)
_GEMINI_SF_RE = re.compile(
    r"^Men.s World Cup Semifinals Qualifiers\? - (?P<team>.+)$", re.IGNORECASE
)

_RUNG_KEY = {"quarterfinals": "qf", "semifinals": "sf"}


@dataclass
class TeamLadder:
    """One team's advance-probability ladder (each rung a (price, volume))."""

    qf: tuple[float, float] | None = None        # P(reach quarterfinals), pm
    sf: tuple[float, float] | None = None        # P(reach semifinals), pm
    win: tuple[float, float] | None = None       # P(win the cup), pm
    sf_gemini: tuple[float, float] | None = None  # P(reach semifinals), gemini


def parse_ladders(rows: list[Mkt]) -> dict[str, TeamLadder]:
    """→ {team display name: TeamLadder}. Unpriced rows are skipped; a
    confederation row never matches (the "(UEFA)"-style parenthetical is
    excluded by the team-name character class)."""
    ladders: dict[str, TeamLadder] = {}

    def _put(team: str, attr: str, m: Mkt) -> None:
        if m.price is None:
            return
        entry = ladders.setdefault(team.strip(), TeamLadder())
        setattr(entry, attr, (float(m.price), float(m.volume_24h or 0.0)))

    for m in rows:
        if m.venue == "polymarket":
            if match := _PM_LADDER_RE.match(m.title):
                _put(match["team"], _RUNG_KEY[match["rung"].lower()], m)
            elif match := _PM_WIN_RE.match(m.title):
                _put(match["team"], "win", m)
        elif m.venue == "gemini":
            if match := _GEMINI_SF_RE.match(m.title):
                _put(match["team"], "sf_gemini", m)
    return ladders


# ---------------------------------------------------------------------------
# Fixture (match table) parsing — derived from the direct advance markets
# ---------------------------------------------------------------------------

_PM_DIRECT_RE = re.compile(
    r"^(?P<a>[^:]+?)\s+vs\.?\s+(?P<b>[^:]+?):\s*Team to Advance$", re.IGNORECASE
)
_KALSHI_DIRECT_RE = re.compile(
    r"^(?P<a>[^:]+?)\s+vs\.?\s+(?P<b>[^:]+?):\s*To Advance - (?P<x>.+?) advances$",
    re.IGNORECASE,
)


@dataclass
class Fixture:
    """One matchup. ``a``/``b`` keep the venue's first-named orientation —
    every price in this dataclass is P(a advances)."""

    a: str
    b: str
    pm_direct: tuple[float, float] | None = None      # (P(a), volume)
    kalshi_legs: dict[str, tuple[float, float]] = field(default_factory=dict)
    # ^ team → (raw leg price, volume); both legs → vig-strippable pair


def parse_fixtures(rows: list[Mkt], teams: set[str]) -> list[Fixture]:
    """Match table from the direct markets. A pair only counts when BOTH
    names are ladder teams — that gate drops cricket/other-sport "vs …
    advances" phrasings without any sport-specific denylist."""
    by_pair: dict[frozenset[str], Fixture] = {}

    def _fixture(a: str, b: str) -> Fixture | None:
        a, b = a.strip(), b.strip()
        if a not in teams or b not in teams:
            return None
        return by_pair.setdefault(frozenset((a, b)), Fixture(a=a, b=b))

    for m in rows:
        if m.price is None:
            continue
        if m.venue == "polymarket" and (match := _PM_DIRECT_RE.match(m.title)):
            fix = _fixture(match["a"], match["b"])
            if fix is not None:
                # Adopt polymarket's orientation as canonical for the pair.
                if fix.a != match["a"].strip():
                    fix.a, fix.b = fix.b, fix.a
                    fix.kalshi_legs = dict(fix.kalshi_legs)
                fix.pm_direct = (float(m.price), float(m.volume_24h or 0.0))
        elif m.venue == "kalshi" and (match := _KALSHI_DIRECT_RE.match(m.title)):
            fix = _fixture(match["a"], match["b"])
            if fix is not None:
                fix.kalshi_legs[match["x"].strip()] = (
                    float(m.price),
                    float(m.volume_24h or 0.0),
                )
    return sorted(by_pair.values(), key=lambda f: (f.a, f.b))


# ---------------------------------------------------------------------------
# Round inference + per-matchup odds
# ---------------------------------------------------------------------------

# A knockout pair's two advance prices should sum to 1; real books carry vig /
# staleness so accept a generous band. Outside the band the rung is not this
# fixture's price and must not be used as a source.
_COMPLEMENT_BAND = (0.85, 1.20)


def _rung_pair(
    ladders: dict[str, TeamLadder], fix: Fixture, attr: str
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    la, lb = ladders.get(fix.a), ladders.get(fix.b)
    qa = getattr(la, attr, None) if la else None
    qb = getattr(lb, attr, None) if lb else None
    if qa is None or qb is None:
        return None
    lo, hi = _COMPLEMENT_BAND
    return (qa, qb) if lo <= qa[0] + qb[0] <= hi else None


def infer_round(ladders: dict[str, TeamLadder], fix: Fixture) -> tuple[str, str | None]:
    """→ (round label, ladder rung that prices this matchup).

    reach-QF prices complementary → the teams are playing the R16 tie whose
    winner reaches the QF. Else reach-SF complementary → both are already
    quarterfinalists and the SF rung prices the tie. Else unknown (direct
    markets may still price it)."""
    if _rung_pair(ladders, fix, "qf") is not None:
        return "R16", "qf"
    if _rung_pair(ladders, fix, "sf") is not None:
        return "QF", "sf"
    return "TBD", None


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    total_w = sum(w for _, w in pairs)
    if total_w > 0:
        return sum(p * w for p, w in pairs) / total_w
    return sum(p for p, _ in pairs) / len(pairs)


def _odds(p: float) -> float | None:
    """Decimal odds; None when the outcome is priced at 0 (no finite payout)."""
    return round(1.0 / p, 2) if p > 0 else None


def build_matchup(
    ladders: dict[str, TeamLadder],
    fix: Fixture,
    *,
    sweet_odds: float,
    min_edge: float,
) -> dict:
    """One matchup row: per-source P(a advances), volume-weighted consensus,
    decimal odds both sides, cross-source edge and the sweet flags."""
    round_label, rung = infer_round(ladders, fix)
    sources: list[dict] = []

    if fix.pm_direct is not None:
        p, vol = fix.pm_direct
        sources.append(
            {"source": "polymarket:direct", "p_a": round(p, 4), "volume_24h": vol}
        )
    if fix.kalshi_legs:
        leg_a, leg_b = fix.kalshi_legs.get(fix.a), fix.kalshi_legs.get(fix.b)
        if leg_a is not None and leg_b is not None and (leg_a[0] + leg_b[0]) > 0:
            p = leg_a[0] / (leg_a[0] + leg_b[0])  # vig-stripped two-leg book
            vol = min(leg_a[1], leg_b[1])
            sources.append(
                {
                    "source": "kalshi:direct",
                    "p_a": round(p, 4),
                    "volume_24h": vol,
                    "book_sum": round(leg_a[0] + leg_b[0], 4),
                }
            )
        elif leg_a is not None:
            sources.append(
                {"source": "kalshi:direct", "p_a": round(leg_a[0], 4), "volume_24h": leg_a[1]}
            )
    if rung is not None and (pair := _rung_pair(ladders, fix, rung)) is not None:
        (pa, va), (pb, vb) = pair
        sources.append(
            {
                "source": f"polymarket:ladder:{rung}",
                "p_a": round(pa / (pa + pb), 4),
                "volume_24h": min(va, vb),
                "book_sum": round(pa + pb, 4),
            }
        )
    if rung == "sf" and (pair := _rung_pair(ladders, fix, "sf_gemini")) is not None:
        (pa, va), (pb, vb) = pair
        sources.append(
            {
                "source": "gemini:ladder:sf",
                "p_a": round(pa / (pa + pb), 4),
                "volume_24h": min(va, vb),
                "book_sum": round(pa + pb, 4),
            }
        )

    if not sources:
        return {}

    p_a = _weighted_mean([(s["p_a"], s["volume_24h"] or 0.0) for s in sources])
    edge = (
        round(max(s["p_a"] for s in sources) - min(s["p_a"] for s in sources), 4)
        if len(sources) > 1
        else 0.0
    )
    odds_a, odds_b = _odds(p_a), _odds(1.0 - p_a)

    reasons: list[str] = []
    if max(o for o in (odds_a, odds_b) if o is not None) >= sweet_odds:
        reasons.append("underdog_odds")
    if edge >= min_edge:
        reasons.append("cross_source_edge")

    # R16 lookahead: P(win the NEXT round | advance) = P(reach SF)/P(reach QF)
    # — the ladder ratio, the market's own conditional read on the QF tie.
    lookahead: dict[str, float] = {}
    if rung == "qf":
        for team in (fix.a, fix.b):
            entry = ladders.get(team)
            if entry and entry.qf and entry.sf and entry.qf[0] > 0:
                lookahead[team] = round(min(1.0, entry.sf[0] / entry.qf[0]), 4)

    return {
        "round": round_label,
        "team_a": fix.a,
        "team_b": fix.b,
        "p_a": round(p_a, 4),
        "p_b": round(1.0 - p_a, 4),
        "odds_a": odds_a,
        "odds_b": odds_b,
        "edge": edge,
        "sweet": bool(reasons),
        "sweet_reasons": reasons,
        "sources": sources,
        "lookahead_win_next": lookahead or None,
    }


def build_board(rows: list[Mkt], *, sweet_odds: float, min_edge: float) -> dict:
    """Full payload body: matchup rows (sweet first, then by round) + the raw
    team ladder table for the drill-down column."""
    ladders = parse_ladders(rows)
    fixtures = parse_fixtures(rows, set(ladders))
    matchups = [
        row
        for fix in fixtures
        if (row := build_matchup(ladders, fix, sweet_odds=sweet_odds, min_edge=min_edge))
    ]
    matchups.sort(key=lambda r: (not r["sweet"], -r["edge"], r["team_a"]))
    teams = {
        team: {
            "qf": entry.qf[0] if entry.qf else None,
            "sf": entry.sf[0] if entry.sf else None,
            "win": entry.win[0] if entry.win else None,
            "sf_gemini": entry.sf_gemini[0] if entry.sf_gemini else None,
        }
        for team, entry in sorted(ladders.items())
    }
    return {
        "matchups": matchups,
        "teams": teams,
        "params": {"sweet_odds": sweet_odds, "min_edge": min_edge},
    }
