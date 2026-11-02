"""Unit tests for pmi_api.wc_matchups (pure logic — no DB, no app).

Fixture prices mirror the real 2026-07-07 book so the expectations double as
documentation of the inference rules:

  * France/Morocco reach-SF .79/.222 (sum 1.01)  → QF fixture, SF rung prices it
  * Argentina/Egypt reach-QF .86/.15 (sum 1.01)  → R16 fixture, QF rung
  * Norway advance: polymarket .35 vs kalshi .38/.62 legs → cross-source edge
"""

from __future__ import annotations

from pmi_api.wc_matchups import (
    Mkt,
    build_board,
    build_matchup,
    infer_round,
    parse_fixtures,
    parse_ladders,
)


def _mkt(venue: str, title: str, price: float | None, vol: float = 1000.0) -> Mkt:
    return Mkt(market_id=hash(title) % 10_000, venue=venue, title=title, price=price, volume_24h=vol)


LADDER_ROWS = [
    _mkt("polymarket", "Will France reach the Semifinals at the 2026 FIFA World Cup?", 0.79),
    _mkt("polymarket", "Will Morocco reach the Semifinals at the 2026 FIFA World Cup?", 0.222),
    _mkt("polymarket", "Will Argentina reach the Semifinals at the 2026 FIFA World Cup?", 0.64),
    _mkt("polymarket", "Will Egypt reach the Semifinals at the 2026 FIFA World Cup?", 0.033),
    _mkt("polymarket", "Will Argentina reach the Quarterfinals at the 2026 FIFA World Cup?", 0.86),
    _mkt("polymarket", "Will Egypt reach the Quarterfinals at the 2026 FIFA World Cup?", 0.15),
    _mkt("polymarket", "Will France win the 2026 FIFA World Cup?", 0.33),
    # Confederation row must NOT become a team (parenthetical blocked).
    _mkt("polymarket", "Will Europe (UEFA) win the 2026 FIFA World Cup?", 0.77),
    # gemini SF board (fresh quotes for the same fixture teams).
    _mkt("gemini", "Men's World Cup Semifinals Qualifiers? - France", 0.79),
    _mkt("gemini", "Men's World Cup Semifinals Qualifiers? - Morocco", 0.25),
    # Unpriced gemini row is skipped entirely.
    _mkt("gemini", "Men's World Cup Semifinals Qualifiers? - Curacao", None),
]

DIRECT_ROWS = [
    _mkt("polymarket", "France vs. Morocco: Team to Advance", 0.78, vol=331_429),
    _mkt("polymarket", "Argentina vs. Egypt: Team to Advance", 0.86, vol=1_583_957),
    # kalshi legs (no period after "vs", per real titles).
    _mkt("kalshi", "France vs Morocco: To Advance - Morocco advances", 0.23, vol=50_000),
    _mkt("kalshi", "France vs Morocco: To Advance - France advances", 0.78, vol=60_000),
    # Cricket phrasing with a non-ladder team must be dropped by the team gate.
    _mkt("kalshi", "England vs India: To Advance - India advances", 0.55),
]


def test_parse_ladders_rungs_and_confederation_exclusion():
    ladders = parse_ladders(LADDER_ROWS)
    assert ladders["France"].sf == (0.79, 1000.0)
    assert ladders["Argentina"].qf == (0.86, 1000.0)
    assert ladders["France"].win == (0.33, 1000.0)
    assert ladders["France"].sf_gemini == (0.79, 1000.0)
    assert "Europe (UEFA)" not in ladders and "Europe" not in ladders
    assert "Curacao" not in ladders  # unpriced → skipped


def test_parse_fixtures_orientation_and_team_gate():
    ladders = parse_ladders(LADDER_ROWS)
    fixtures = parse_fixtures(DIRECT_ROWS, set(ladders))
    pairs = {(f.a, f.b) for f in fixtures}
    assert ("France", "Morocco") in pairs      # polymarket orientation kept
    assert ("Argentina", "Egypt") in pairs
    assert not any("India" in p for p in pairs)  # non-ladder team gated out
    fm = next(f for f in fixtures if f.a == "France")
    assert fm.pm_direct == (0.78, 331_429.0)
    assert fm.kalshi_legs["France"] == (0.78, 60_000.0)


def test_infer_round_from_ladder_complementarity():
    ladders = parse_ladders(LADDER_ROWS)
    fixtures = {f.a: f for f in parse_fixtures(DIRECT_ROWS, set(ladders))}
    assert infer_round(ladders, fixtures["Argentina"]) == ("R16", "qf")  # .86+.15≈1
    assert infer_round(ladders, fixtures["France"]) == ("QF", "sf")     # .79+.222≈1


def test_matchup_sources_vig_strip_and_lookahead():
    ladders = parse_ladders(LADDER_ROWS)
    fixtures = {f.a: f for f in parse_fixtures(DIRECT_ROWS, set(ladders))}
    row = build_matchup(ladders, fixtures["France"], sweet_odds=3.0, min_edge=0.03)
    by_src = {s["source"]: s for s in row["sources"]}
    # kalshi pair vig-stripped: .78/(.78+.23) = .7723
    assert by_src["kalshi:direct"]["p_a"] == 0.7723
    assert by_src["kalshi:direct"]["book_sum"] == 1.01
    # ladder rung normalised: .79/(.79+.222)
    assert by_src["polymarket:ladder:sf"]["p_a"] == 0.7806
    assert "gemini:ladder:sf" in by_src
    # QF fixture → no lookahead; R16 fixture → ladder ratio P(SF)/P(QF)
    assert row["lookahead_win_next"] is None
    r16 = build_matchup(ladders, fixtures["Argentina"], sweet_odds=3.0, min_edge=0.03)
    assert r16["lookahead_win_next"]["Argentina"] == round(0.64 / 0.86, 4)


def test_sweet_flags_underdog_and_edge_thresholds():
    ladders = parse_ladders(LADDER_ROWS)
    fixtures = {f.a: f for f in parse_fixtures(DIRECT_ROWS, set(ladders))}
    fr = build_matchup(ladders, fixtures["France"], sweet_odds=3.0, min_edge=0.03)
    # Morocco side pays 1/.22 ≈ 4.5 → underdog_odds fires at 3.0 …
    assert "underdog_odds" in fr["sweet_reasons"]
    # … but not when the bar is above the payout.
    fr_hi = build_matchup(ladders, fixtures["France"], sweet_odds=6.0, min_edge=0.5)
    assert fr_hi["sweet"] is False
    # Edge = max−min across sources; tighten min_edge to flip the flag.
    assert fr["edge"] > 0
    fr_edge = build_matchup(
        ladders, fixtures["France"], sweet_odds=1000.0, min_edge=fr["edge"]
    )
    assert fr_edge["sweet_reasons"] == ["cross_source_edge"]


def test_build_board_sorts_sweet_first():
    rows = LADDER_ROWS + DIRECT_ROWS
    board = build_board(rows, sweet_odds=3.0, min_edge=0.03)
    assert [m["sweet"] for m in board["matchups"]] == sorted(
        [m["sweet"] for m in board["matchups"]], reverse=True
    )
    assert board["teams"]["France"]["sf"] == 0.79
    assert board["params"] == {"sweet_odds": 3.0, "min_edge": 0.03}
