"""Unit tests for pmi_api.esports_matches (pure logic — no DB, no app).

Covers the fold's contract: vig-strip a Kalshi pair, read Polymarket's Match
Winner, combine the two venues on one match (with the cross-venue edge), tolerate
the ~2-day cross-venue close-time offset, strip team-name suffixes, drop an
un-opened (0/0) book, and refuse to fold two venues that disagree wildly (a
finished game vs an upcoming rematch).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pmi_core.engine.esports_matches import (
    Mkt,
    _team_key,
    _ticker_match_time,
    build_matches,
    favourite_market,
)

_MATCH = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)  # real match start
_BASE = _MATCH  # back-compat alias; equals the real match start
# Kalshi/Robinhood close ~2 days AFTER the match (a venue convention); Polymarket
# closes ~at match time. Clustering keys on the real match start, not these.
_KCLOSE = _MATCH + timedelta(days=2)
_MONS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_ET = ZoneInfo("America/New_York")


def _tk_dt(t):
    # Kalshi stamps the ticker in US Eastern wall-clock; convert the test's UTC match
    # time to ET before stamping so _ticker_match_time (ET→UTC) round-trips to `t`.
    et = t.astimezone(_ET)
    return f"{et.year % 100:02d}{_MONS[et.month - 1]}{et.day:02d}{et.hour:02d}{et.minute:02d}"


def _kalshi(mid, a, b, winner, price, *, match=_MATCH, closes=None, game="CS2", ev="EV1",
            code="", vol=100.0, rules=None, match_time=None):
    # Ticker carries the real match datetime then the matchup; `-{code}` is the
    # winning team's short code. Close defaults to match + 2 days (venue convention).
    return Mkt(
        market_id=mid,
        venue="kalshi",
        title=f"Will {winner} win the {a} vs. {b} {game} match?",
        external_id=f"KX{game}GAME-{_tk_dt(match)}{ev}-{code}",
        event_key=ev,
        group_item=None,
        price=price,
        volume_24h=vol,
        closes_at=closes if closes is not None else match + timedelta(days=2),
        url=None,
        rules=rules,
        match_time=match_time,
    )


def _poly(mid, a, b, price, *, match=_MATCH, closes=None, game="Counter-Strike",
          tourney="Some Cup", match_time=None):
    # Polymarket's numeric id carries no datetime; its close is ~at match time.
    return Mkt(
        market_id=mid,
        venue="polymarket",
        title=f"{game}: {a} vs {b} (BO3) - {tourney}",
        external_id=str(mid),  # numeric id — no team code
        event_key=None,
        group_item="Match Winner",
        price=price,
        volume_24h=100.0,
        closes_at=closes if closes is not None else match,
        url=None,
        match_time=match_time,
    )


def _kalshi_new(mid, a, b, winner, price, *, match=_MATCH, closes=None, game="LOL", ev="EVN",
                code="", vol=100.0, rules=None, match_time=None):
    # Kalshi's NEW format (2026-08): the title is just "{winner} wins", the team is
    # in yes_sub_title, and the game/matchup live only in the KX{game}GAME ticker.
    # `a`/`b` are unused in the title — kept so tests read like the old helper.
    return Mkt(
        market_id=mid,
        venue="kalshi",
        title=f"{winner} wins",
        external_id=f"KX{game}GAME-{_tk_dt(match)}{ev}-{code}",
        event_key=ev,
        group_item=None,
        price=price,
        volume_24h=vol,
        closes_at=closes if closes is not None else match + timedelta(days=2),
        url=None,
        rules=rules,
        match_time=match_time,
        yes_sub=winner,
    )


def _robinhood(mid, a, b, winner, price, *, match=_MATCH, ev="RHEV", vol=100.0):
    # Robinhood keeps the old "Will W win the A vs B <game> match?" grammar; its
    # UUID id carries no datetime, so its close (match + 2d) is the only time signal.
    return Mkt(
        market_id=mid,
        venue="robinhood",
        title=f"Will {winner} win the {a} vs {b} League of Legends match?",
        external_id=f"rh-{mid}",
        event_key=ev,
        group_item=None,
        price=price,
        volume_24h=vol,
        closes_at=match + timedelta(days=2),
        url=None,
    )


def _one(rows):
    ms = build_matches(rows)
    assert len(ms) == 1, ms
    return ms[0]


def test_kalshi_new_yes_sub_format_folds():
    # New format: teams from yes_sub_title, game from KXLOLGAME ticker prefix.
    m = _one([
        _kalshi_new(1, "Alpha", "Bravo", "Alpha", 0.60, code="ALP"),
        _kalshi_new(2, "Alpha", "Bravo", "Bravo", 0.40, code="BRV"),
    ])
    assert m["venues"] == ["kalshi"]
    assert m["game"] == "lol"
    assert m["team_a"] == "Alpha"
    assert m["prob_a"] == 60.0  # 0.60 / (0.60 + 0.40)
    assert m["favorite"] == "Alpha"
    assert m["team_a_abbr"] == "ALP" and m["team_b_abbr"] == "BRV"


def test_robinhood_old_grammar_still_folds():
    # Regression: the legacy title parser must still work for Robinhood.
    m = _one([
        _robinhood(1, "Alpha", "Bravo", "Alpha", 0.60),
        _robinhood(2, "Alpha", "Bravo", "Bravo", 0.40),
    ])
    assert m["venues"] == ["robinhood"]
    assert m["game"] == "lol"
    assert m["prob_a"] == 60.0


def test_empty_opposing_leg_does_not_inflate_to_100():
    # NiP wins=0.66, opponent's leg=0.0 (empty book, no bids) → read 66, NOT the
    # vig-inflated 100 that 0.66/(0.66+0.0) used to give.
    m = _one([
        _kalshi_new(1, "NiP", "EDG", "NiP", 0.66, code="NIP"),
        _kalshi_new(2, "NiP", "EDG", "EDG", 0.0, code="EDG"),
    ])
    assert m["prob_a"] == 66.0
    assert m["favorite"] == "NiP"


def test_decided_leg_still_reads_100():
    # A genuinely decided match (winner ~1.0, loser 0.0) must still read ~100.
    m = _one([
        _kalshi_new(1, "NiP", "EDG", "NiP", 0.99, code="NIP"),
        _kalshi_new(2, "NiP", "EDG", "EDG", 0.0, code="EDG"),
    ])
    assert m["prob_a"] == 99.0


def test_new_kalshi_and_poly_merge_when_edge_small():
    # With the empty-leg inflation fixed, kalshi (66) and polymarket (70) agree, so
    # they fold into ONE 2-exchange match instead of splitting on a fake >25 edge.
    ms = build_matches([
        _kalshi_new(1, "NiP", "EDG", "NiP", 0.66, code="NIP"),
        _kalshi_new(2, "NiP", "EDG", "EDG", 0.0, code="EDG"),
        _poly(3, "NiP", "EDG", 0.70, game="LoL"),
    ])
    assert len(ms) == 1, ms
    assert ms[0]["venues"] == ["kalshi", "polymarket"]
    assert ms[0]["edge"] < 25


def test_kalshi_pair_vig_stripped_single_venue():
    m = _one([_kalshi(1, "Alpha", "Bravo", "Alpha", 0.55), _kalshi(2, "Alpha", "Bravo", "Bravo", 0.50)])
    assert m["venues"] == ["kalshi"]
    # 0.55 / (0.55 + 0.50) = 0.5238 → 52.4
    assert m["prob_a"] == 52.4
    assert m["favorite"] == "Alpha"
    assert m["edge"] == 0.0


def test_polymarket_match_winner_parsed():
    m = _one([_poly(1, "Alpha", "Bravo", 0.70)])
    assert m["venues"] == ["polymarket"]
    assert m["prob_a"] == 70.0
    assert m["tournament"] == "Some Cup"


def test_cross_venue_fold_combines_and_edges():
    rows = [
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.60),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.40),  # kalshi P(Alpha)=0.60
        _poly(3, "Alpha", "Bravo", 0.65, closes=_MATCH + timedelta(hours=6)),  # poly closes ~at match
    ]
    m = _one(rows)
    assert m["venues"] == ["kalshi", "polymarket"]
    # Volume-weighted: the Kalshi reading carries both winner markets' volume
    # (200) vs Polymarket's one (100), so (60·200 + 65·100) / 300 = 61.7.
    assert m["prob_a"] == 61.7
    assert m["edge"] == 5.0
    assert {s["venue"] for s in m["sources"]} == {"kalshi", "polymarket"}


def test_team_suffix_and_order_align_across_venues():
    # Kalshi "Alpha Esports", Polymarket "Alpha" with the pair named B-then-A.
    rows = [
        _kalshi(1, "Alpha Esports", "Bravo Gaming", "Alpha Esports", 0.80),
        _kalshi(2, "Alpha Esports", "Bravo Gaming", "Bravo Gaming", 0.20),
        _poly(3, "Bravo", "Alpha", 0.25),  # P(Bravo)=0.25 → P(Alpha)=0.75
    ]
    m = _one(rows)
    assert m["venues"] == ["kalshi", "polymarket"]
    # both read Alpha ~0.80 / ~0.75 → combined ~77.5, small edge, one match
    assert m["favorite"].startswith("Alpha")
    assert m["edge"] <= 6.0


def test_unopened_kalshi_book_is_dropped():
    # 0.00 / 0.00 = an un-opened Kalshi book → contributes no reading.
    assert build_matches([_kalshi(1, "A", "B", "A", 0.0), _kalshi(2, "A", "B", "B", 0.0)]) == []


def test_team_abbreviation_from_kalshi_ticker():
    rows = [
        _kalshi(1, "Team Vitality", "GIANTX", "Team Vitality", 0.60, code="VIT"),
        _kalshi(2, "Team Vitality", "GIANTX", "GIANTX", 0.40, code="GX"),
    ]
    m = _one(rows)
    assert m["team_a"] == "Team Vitality"
    assert {m["team_a_abbr"], m["team_b_abbr"]} == {"VIT", "GX"}


def test_abbr_falls_back_to_full_name_without_ticker():
    # Polymarket's numeric external_id carries no code → the full name stands in.
    m = _one([_poly(1, "Alpha", "Bravo", 0.70)])
    assert m["team_a_abbr"] == "Alpha"


def test_wild_disagreement_splits_back_to_single_venue():
    # Kalshi P(Alpha)=0.90 vs Polymarket P(Alpha)=0.20 → not the same match.
    rows = [
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.90),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.10),
        _poly(3, "Alpha", "Bravo", 0.20, closes=_BASE + timedelta(hours=10)),
    ]
    ms = build_matches(rows)
    assert len(ms) == 2
    assert all(len(m["venues"]) == 1 for m in ms)
    assert {m["venues"][0] for m in ms} == {"kalshi", "polymarket"}


def test_two_same_venue_books_collapse_no_edge():
    # A venue can list two markets for the SAME match (a re-listing / second map) —
    # same ticker match time, so they cluster together. They must fold to ONE
    # per-venue source with a volume-weighted price — never a fake cross-"exchange"
    # edge (regression: a low-volume duplicate book manufactured a ~57pt edge).
    rows = [
        # real, heavily-traded book: P(Alpha) = 0.07
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.07, ev="REAL", vol=2500),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.93, ev="REAL", vol=2500),
        # thin duplicate of the same match disagreeing wildly: P(Alpha) = 0.64
        _kalshi(3, "Alpha", "Bravo", "Alpha", 0.64, ev="DUP", vol=25),
        _kalshi(4, "Alpha", "Bravo", "Bravo", 0.36, ev="DUP", vol=25),
    ]
    m = _one(rows)
    assert m["venues"] == ["kalshi"]
    assert len(m["sources"]) == 1  # one per venue, not two raw books
    assert m["edge"] == 0.0  # same venue → no cross-venue spread
    # volume-weighted (5000 vs 50) → dominated by the real book, not the 35.5 mean
    assert m["prob_a"] == 7.6


def test_kalshi_tournament_from_rules():
    # Kalshi has no tournament in the title — it's in the resolution rules.
    rules = "If Alpha wins the Cool Cup 2026: Alpha vs. Bravo CS2 match originally scheduled for..."
    rows = [
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.60, rules=rules),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.40, rules=rules),
    ]
    assert _one(rows)["tournament"] == "Cool Cup 2026"


def test_polymarket_match_borrows_kalshi_abbrev():
    # A team seen on Kalshi (with a ticker code) lends its short code to a
    # Polymarket-only match; a team never on Kalshi keeps its full name.
    rows = [
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.60, code="ALP"),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.40, code="BRV"),
        _poly(3, "Alpha", "Charlie", 0.70),  # different pair, Polymarket only
    ]
    poly = next(m for m in build_matches(rows) if m["venues"] == ["polymarket"])
    assert poly["team_a_abbr"] == "ALP"
    assert poly["team_b_abbr"] == "Charlie"


def test_tier_markers_are_not_stripped_from_team_key():
    # A farm/academy squad must key differently from the main roster, else two
    # distinct matches (e.g. LCK Challengers vs KeSPA main) fold into one card.
    assert _team_key("DN SOOPers Challengers") != _team_key("DN SOOPers")
    assert _team_key("Nongshim Esports Academy") != _team_key("Nongshim Red Force")
    assert _team_key("Nongshim Esports Academy") != _team_key("Nongshim")
    # but generic org-suffix noise is still stripped so cross-venue names match
    assert _team_key("Dragon Ranger Gaming") == _team_key("Dragon Ranger")


def test_favourite_market_picks_highest_volume_oriented_to_fav():
    rows = [
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.70, vol=900),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.30, vol=100),
    ]
    m = _one(rows)
    assert m["favorite"] == "Alpha"
    # highest-volume winner leg is #1 (Alpha), and its price reads P(favourite).
    assert favourite_market(rows, m) == (1, True)


def test_favourite_market_polymarket_orientation_flip():
    # Poly price = P(Alpha)=0.30 → favourite is Bravo, so the market is NOT
    # favourite-oriented (trend must use 1 - price).
    rows = [_poly(9, "Alpha", "Bravo", 0.30)]
    m = _one(rows)
    assert m["favorite"] == "Bravo"
    assert favourite_market(rows, m) == (9, False)


def test_match_has_readable_slug():
    # game-teamA-vs-teamB-date, slugified; date from _BASE (2026-08-20).
    m = _one([
        _kalshi(1, "Noir Verse", "ex-RUSTEC", "Noir Verse", 0.60),
        _kalshi(2, "Noir Verse", "ex-RUSTEC", "ex-RUSTEC", 0.40),
    ])
    assert m["slug"] == "cs2-noir-verse-vs-ex-rustec-aug20"


def test_starts_at_is_real_match_start_not_close():
    # Kalshi's close is ~2 days past the match; starts_at (and the slug date) must be
    # the real match start — from the ticker for Kalshi, from the close for Polymarket
    # (which closes ~at match time) — never the +2d Kalshi close.
    kalshi = _one([
        _kalshi(1, "Alpha", "Bravo", "Alpha", 0.60),
        _kalshi(2, "Alpha", "Bravo", "Bravo", 0.40),
    ])
    assert kalshi["starts_at"] == _MATCH.isoformat()
    poly = _one([_poly(9, "Gamma", "Delta", 0.55)])
    assert poly["starts_at"] == _MATCH.isoformat()


def test_ticker_match_time_parsed():
    # The real match datetime is encoded in the Kalshi ticker (US Eastern wall-clock),
    # not a JSON field. "26OCT171400" = Oct 17 1 PM… 2 PM ET = 18:00 UTC (EDT, -4).
    assert _ticker_match_time("KXLOLGAME-26OCT171400FURMVK-FUR") == datetime(
        2026, 10, 17, 18, 0, tzinfo=UTC
    )
    # No datetime in Polymarket numeric ids or Robinhood UUIDs.
    assert _ticker_match_time("5838886") is None
    assert _ticker_match_time("rh-42") is None
    assert _ticker_match_time(None) is None


def test_rematch_two_days_apart_splits_into_two():
    # A losers-bracket rematch of the same pair 2 days later. Kalshi closes are
    # match + 2d, so the two overlap on the close-time axis — only the ticker match
    # time separates them. Must yield TWO matches, not one merged card (regression:
    # the Worlds Play-In MVK vs FURIA rematch vanished from the bracket this way).
    d1 = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
    d2 = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)
    ms = build_matches([
        _kalshi(1, "MVK", "FURIA", "MVK", 0.60, match=d1, ev="M1", code="MVK"),
        _kalshi(2, "MVK", "FURIA", "FURIA", 0.40, match=d1, ev="M1", code="FUR"),
        _kalshi(3, "MVK", "FURIA", "MVK", 0.55, match=d2, ev="M2", code="MVK"),
        _kalshi(4, "MVK", "FURIA", "FURIA", 0.45, match=d2, ev="M2", code="FUR"),
    ])
    assert len(ms) == 2, ms
    assert {m["starts_at"] for m in ms} == {d1.isoformat(), d2.isoformat()}


def test_same_match_cross_venue_still_folds():
    # The flip side: one real match's Kalshi (ticker time T) and Polymarket (closes
    # ~T) reads must still fold into a single cross-venue card, not split.
    t = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
    ms = build_matches([
        _kalshi(1, "KC", "MVK", "KC", 0.60, match=t, game="LOL", code="KC"),
        _kalshi(2, "KC", "MVK", "MVK", 0.40, match=t, game="LOL", code="MVK"),
        _poly(3, "KC", "MVK", 0.63, closes=t + timedelta(hours=8), game="LoL"),
    ])
    assert len(ms) == 1, ms
    assert ms[0]["venues"] == ["kalshi", "polymarket"]
