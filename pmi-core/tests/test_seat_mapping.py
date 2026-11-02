"""Unit tests for market → contested-seat mapping (CORR-1.3)."""

from __future__ import annotations

import pytest

from pmi_core.engine.seat_mapping import (
    candidate_party,
    extract_contested_seats,
    parse_candidate_race,
    parse_seat_race,
    resolve_candidate_party,
    state_code,
)

# --------------------------------------------------------------------------
# parse_seat_race — only party-direct per-state races match
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Will the Republicans win the Ohio Senate race in 2026?", ("R", "Ohio")),
        ("Will the Democrats win the Texas Senate race in 2026?", ("D", "Texas")),
        (
            "Will the Republicans win the North Carolina Senate race in 2026?",
            ("R", "North Carolina"),
        ),
        (
            "Will the Republicans win the New Hampshire Senate race in 2026?",
            ("R", "New Hampshire"),
        ),
    ],
)
def test_parse_matches_party_direct_races(title, expected):
    assert parse_seat_race(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        # forecastex party-direct
        (
            "Will the Republican party win the Texas general election for US Senate in 2026?",
            ("R", "Texas"),
        ),
        (
            "Will the Democratic party win the New Hampshire general election for US Senate in 2026?",
            ("D", "New Hampshire"),
        ),
        # forecastex party-proxy (primary winner IS the nominee)
        (
            "Will Democratic Primary Winner win the Wyoming general election for US Senate in 2026?",
            ("D", "Wyoming"),
        ),
        (
            "Will Republican Primary Winner win the Wyoming general election for US Senate in 2026?",
            ("R", "Wyoming"),
        ),
        # predictit per-leg
        (
            "Which party will win the 2026 US Senate election in Maine? - Republican",
            ("R", "Maine"),
        ),
        (
            "Which party will win the 2026 US Senate election in New Mexico? - Democratic",
            ("D", "New Mexico"),
        ),
        # kalshi party-aggregate outcome leg (yearless; party off the leg, not
        # the misspelled "Democratics" subject; "(DFL)" tolerated)
        (
            "Will Democratics win the Senate race in Alabama? - Democratic party",
            ("D", "Alabama"),
        ),
        (
            "Will Republicans win the Senate race in Alabama? - Republican party",
            ("R", "Alabama"),
        ),
        (
            "Will Democratic (DFL)s win the Senate race in Minnesota? - Democratic (DFL) party",
            ("D", "Minnesota"),
        ),
    ],
)
def test_parse_matches_cross_venue_phrasings(title, expected):
    assert parse_seat_race(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        # nominee / primary markets
        "Will Ed Markey be the Democratic nominee for Senate in Massachusetts?",
        "Will Ken Paxton win the Texas Republican Senate Primary runoff by 9% or more?",
        # candidate-name general-election (handled by parse_candidate_race)
        "Will Dan Sullivan win the Alaska Senate race in 2026?",
        # independent (deferred)
        "Will an independent win the Montana Senate race in 2026?",
        # foreign senates
        "Will Acción Popular (AP) win the most seats in the 2026 Peruvian Senate election?",
        # chamber-level brackets / control / leadership
        "Will the Republican Party hold 47 or fewer Senate seats after the 2026 midterm elections?",
        "2026 Balance of Power: R Senate, R House",
        "Will Chuck Schumer be the next Senate Majority Leader?",
        # procedural
        "Will the Senate pass a reconciliation bill by June 13?",
        # forecastex primaries (not "general election")
        "Will Buddy Carter win the Georgia Republican primary election for US Senate in 2026?",
        # forecastex chamber-level
        "Will Democrats hold more than 49 seats in the Senate following the 2026 election?",
        # kalshi margin-of-victory / candidate legs — no per-seat reading
        "Will the margin of victory for Republicans in the U.S. Senate election in Wyoming be at least 32 percentage points?",
        "Who will win the 2026 Michigan Senate race? - Haley Stevens",
        # predictit nominee market
        "Who will win the 2026 Minnesota Republican Senate nomination? - Tom Weiler",
        # predictit chamber bracket
        "How many Senate seats will the GOP control after the 2026 midterms? - 49",
    ],
)
def test_parse_rejects_noise(title):
    assert parse_seat_race(title) is None


def test_state_code_lookup():
    assert state_code("Ohio") == "OH"
    assert state_code("north carolina") == "NC"
    assert state_code("New Hampshire") == "NH"
    assert state_code("Notastate") is None


# --------------------------------------------------------------------------
# parse_candidate_race — candidate-name general-election titles
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        (
            "Will Dan Sullivan win the Alaska Senate race in 2026?",
            ("Dan Sullivan", "Alaska"),
        ),
        (
            "Will Mary Peltola win the Alaska Senate race in 2026?",
            ("Mary Peltola", "Alaska"),
        ),
        # title-embedded party tag stays part of the name (candidate_party reads it)
        (
            "Will Dan Sullivan (R) win the Alaska Senate race in 2026?",
            ("Dan Sullivan (R)", "Alaska"),
        ),
        # forecastex general-election tail
        (
            "Will Barry Moore win the Alabama general election for US Senate in 2026?",
            ("Barry Moore", "Alabama"),
        ),
        (
            "Will Mary Peltola win the Alaska general election for US Senate in 2026?",
            ("Mary Peltola", "Alaska"),
        ),
    ],
)
def test_parse_candidate_race_matches(title, expected):
    assert parse_candidate_race(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        # party-direct → belongs to parse_seat_race, not here
        "Will the Republicans win the Ohio Senate race in 2026?",
        "Will the Democrats win the Texas Senate race in 2026?",
        # independent / generic subjects
        "Will an independent win the Montana Senate race in 2026?",
        "Will a Democrat win the Vermont Senate race in 2026?",
        # nominee / primary markets (no "Senate race in 2026" tail)
        "Will Ed Markey be the Democratic nominee for Senate in Massachusetts?",
        "Will Ken Paxton win the Texas Republican Senate Primary runoff by 9% or more?",
        # unrecognised state → foreign senates can't sneak in
        "Will Keiko Fujimori win the Lima Senate race in 2026?",
        # procedural
        "Will the Senate pass a reconciliation bill by June 13?",
        # forecastex party-proxy placeholder → party-direct, not a candidate
        "Will Democratic Primary Winner win the Wyoming general election for US Senate in 2026?",
        # forecastex primaries (no general-election tail)
        "Will Buddy Carter win the Georgia Republican primary election for US Senate in 2026?",
    ],
)
def test_parse_candidate_race_rejects(title):
    assert parse_candidate_race(title) is None


# --------------------------------------------------------------------------
# candidate_party / resolve_candidate_party — party signals
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Jon Husted (R)", "R"),
        ("Sherrod Brown (D)", "D"),
        ("Dan Sullivan (R-AK)", "R"),
        ("Ken Paxton (REP)", "R"),
        ("James Talarico (Dem)", "D"),
        ("Angus King (I)", None),     # independents never resolve
        ("Dan Sullivan", None),       # Alaska's live payloads carry no tag
        ("Republican", None),         # generic groupItemTitle, no parens
        ("", None),
        (None, None),
    ],
)
def test_candidate_party(label, expected):
    assert candidate_party(label) == expected


def test_resolve_candidate_party_prefers_label_over_factor():
    # explicit (D) tag beats a (wrong) republican_on_yes=1
    assert resolve_candidate_party("Roy Cooper (D)", 1.0) == "D"


def test_resolve_candidate_party_falls_back_to_republican_on_yes():
    assert resolve_candidate_party("Dan Sullivan", 1.0) == "R"
    assert resolve_candidate_party(None, 1.0) == "R"


def test_resolve_candidate_party_zero_factor_is_not_democrat():
    # 0.0 covers D / independents / write-ins alike → unresolved, not "D".
    assert resolve_candidate_party("Mary Peltola", 0.0) is None
    assert resolve_candidate_party(None, None) is None


# --------------------------------------------------------------------------
# extract_contested_seats — collapse to one P(R) per state
# --------------------------------------------------------------------------


def test_r_market_used_directly():
    seats = extract_contested_seats(
        [(1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62)]
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.state == "Ohio"
    assert s.state_code == "OH"
    assert s.prob_r == pytest.approx(0.62)
    assert s.market_id == 1
    assert s.source_party == "R"


def test_d_market_flipped():
    seats = extract_contested_seats(
        [(2, "Will the Democrats win the Illinois Senate race in 2026?", 0.80)]
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.prob_r == pytest.approx(0.20)  # 1 - 0.80
    assert s.source_party == "D"
    assert s.market_id == 2


def test_both_markets_collapse_to_one_seat_preferring_r():
    # Texas has both an R and a D market → ONE seat, R price wins.
    seats = extract_contested_seats(
        [
            (10, "Will the Republicans win the Texas Senate race in 2026?", 0.78),
            (11, "Will the Democrats win the Texas Senate race in 2026?", 0.21),
        ]
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.state == "Texas"
    assert s.prob_r == pytest.approx(0.78)
    assert s.source_party == "R"
    assert s.market_id == 10
    # Both underlying markets surface for attribution (CORR-1.3 step 2).
    assert s.r_market_id == 10
    assert s.d_market_id == 11


def test_single_sided_seats_carry_only_their_market():
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
            (2, "Will the Democrats win the Illinois Senate race in 2026?", 0.80),
        ]
    )
    by_state = {s.state: s for s in seats}
    assert by_state["Ohio"].r_market_id == 1
    assert by_state["Ohio"].d_market_id is None
    assert by_state["Illinois"].r_market_id is None
    assert by_state["Illinois"].d_market_id == 2


def test_noise_filtered_out_of_contested_set():
    markets = [
        (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
        (2, "Will Ed Markey be the Democratic nominee for Senate in Massachusetts?", 0.4),
        (3, "Will the Senate pass a reconciliation bill by June 13?", 0.5),
        (4, "2026 Balance of Power: R Senate, R House", 0.3),
        (5, "Will the Democrats win the Texas Senate race in 2026?", 0.21),
    ]
    seats = extract_contested_seats(markets)
    # Only Ohio (R) and Texas (D→flip) survive.
    assert {s.state for s in seats} == {"Ohio", "Texas"}


def test_deterministic_sort_by_state():
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Texas Senate race in 2026?", 0.7),
            (2, "Will the Republicans win the Alabama Senate race in 2026?", 0.9),
            (3, "Will the Republicans win the Ohio Senate race in 2026?", 0.6),
        ]
    )
    assert [s.state for s in seats] == ["Alabama", "Ohio", "Texas"]


def test_clamps_out_of_range_price():
    seats = extract_contested_seats(
        [(1, "Will the Republicans win the Ohio Senate race in 2026?", 1.4)]
    )
    assert seats[0].prob_r == 1.0


def test_empty_input():
    assert extract_contested_seats([]) == []


# --------------------------------------------------------------------------
# extract_contested_seats — candidate-name markets (Alaska gap)
# --------------------------------------------------------------------------


def test_candidate_market_with_r_hint_creates_seat():
    # Alaska: no party-direct market anywhere; Dan Sullivan resolved R via the
    # republican_on_yes factor hint → seat with p_r = his price.
    seats = extract_contested_seats(
        [(38481, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375)],
        party_by_market={38481: "R"},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.state == "Alaska"
    assert s.state_code == "AK"
    assert s.prob_r == pytest.approx(0.375)
    assert s.market_id == 38481
    assert s.source_party == "R"
    assert s.r_market_id == 38481
    assert s.d_market_id is None


def test_candidate_market_party_from_title_tag_needs_no_hint():
    seats = extract_contested_seats(
        [(7, "Will Dan Sullivan (R) win the Alaska Senate race in 2026?", 0.4)]
    )
    assert len(seats) == 1
    assert seats[0].state == "Alaska"
    assert seats[0].prob_r == pytest.approx(0.4)


def test_candidate_d_market_flipped():
    seats = extract_contested_seats(
        [(9, "Will Mary Peltola win the Alaska Senate race in 2026?", 0.55)],
        party_by_market={9: "D"},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.prob_r == pytest.approx(0.45)  # 1 - 0.55
    assert s.source_party == "D"
    assert s.d_market_id == 9


def test_candidate_party_falls_back_to_extraction_hint():
    # D-vs-D race: the candidate parser claims the title, but there's no party
    # tag and republican_on_yes=0.0 leaves party_by_market empty (0.0 can't tell
    # a Democrat from an independent). Without the extraction fallback the market
    # is skipped and the seat vanishes into a ~50 default; with it, the audited
    # extraction party ("D") applies and the 1-P flip happens.
    seats = extract_contested_seats(
        [(11, "Will Mary Peltola win the Alaska Senate race in 2026?", 0.7)],
        party_by_market={},  # republican_on_yes=0.0 → unresolved
        extraction_by_market={11: ("D", "Alaska")},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.source_party == "D"
    assert s.prob_r == pytest.approx(0.3)  # 1 - 0.7, the previously-missing flip
    assert s.market_id == 11


def test_candidate_unresolvable_party_and_no_extraction_is_skipped():
    # Guard the graceful-degradation contract: no tag, no hint, no extraction →
    # still skipped, never guessed.
    seats = extract_contested_seats(
        [(12, "Will Mary Peltola win the Alaska Senate race in 2026?", 0.7)],
    )
    assert seats == []


def test_candidate_r_and_d_collapse_to_one_seat_preferring_r():
    seats = extract_contested_seats(
        [
            (1, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
            (2, "Will Mary Peltola win the Alaska Senate race in 2026?", 0.55),
        ],
        party_by_market={1: "R", 2: "D"},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.prob_r == pytest.approx(0.375)
    assert s.source_party == "R"
    assert s.r_market_id == 1
    assert s.d_market_id == 2


def test_unresolved_candidates_skipped_gracefully():
    # The full Alaska slate: only Sullivan's party resolves; the rest (incl.
    # Peltola without a hint) are skipped, never guessed.
    seats = extract_contested_seats(
        [
            (38477, "Will Richard Grayson win the Alaska Senate race in 2026?", 0.01),
            (38478, "Will Dustin Darden win the Alaska Senate race in 2026?", 0.01),
            (38479, "Will Mary Peltola win the Alaska Senate race in 2026?", 0.55),
            (38480, "Will Ann Diener win the Alaska Senate race in 2026?", 0.01),
            (38481, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
        ],
        party_by_market={38481: "R"},
    )
    assert len(seats) == 1
    assert seats[0].market_id == 38481
    assert seats[0].prob_r == pytest.approx(0.375)


def test_party_direct_market_beats_candidate_markets():
    # Ohio has both styles: the party-direct R market wins, candidate ignored.
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
            (2, "Will Jon Husted (R) win the Ohio Senate race in 2026?", 0.58),
        ]
    )
    assert len(seats) == 1
    assert seats[0].market_id == 1
    assert seats[0].prob_r == pytest.approx(0.62)


def test_highest_priced_candidate_represents_the_party():
    # Two R-resolved candidates in one state → max price is the representative.
    seats = extract_contested_seats(
        [
            (1, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
            (2, "Will Other Sullivan win the Alaska Senate race in 2026?", 0.02),
        ],
        party_by_market={1: "R", 2: "R"},
    )
    assert len(seats) == 1
    assert seats[0].market_id == 1
    assert seats[0].prob_r == pytest.approx(0.375)


def test_candidate_seats_mix_with_party_direct_seats():
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
            (2, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
        ],
        party_by_market={2: "R"},
    )
    assert [s.state for s in seats] == ["Alaska", "Ohio"]
    assert [s.prob_r for s in seats] == [pytest.approx(0.375), pytest.approx(0.62)]


# --------------------------------------------------------------------------
# extract_contested_seats — cross-venue merge (venue priority)
# --------------------------------------------------------------------------


def test_cross_venue_same_seat_prefers_polymarket():
    # Texas trades on all three venues → ONE seat, polymarket's price wins
    # regardless of input order.
    seats = extract_contested_seats(
        [
            (1, "Which party will win the 2026 US Senate election in Texas? - Republican", 0.74),
            (2, "Will the Republican party win the Texas general election for US Senate in 2026?", 0.76),
            (3, "Will the Republicans win the Texas Senate race in 2026?", 0.78),
        ],
        venue_by_market={1: "predictit", 2: "forecastex", 3: "polymarket"},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.state == "Texas"
    assert s.prob_r == pytest.approx(0.78)
    assert s.market_id == 3


def test_cross_venue_fills_states_polymarket_lacks():
    # Wyoming only trades on forecastex (via the primary-winner proxy) →
    # it still becomes a contested seat.
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
            (2, "Will Democratic Primary Winner win the Wyoming general election for US Senate in 2026?", 0.05),
        ],
        venue_by_market={1: "polymarket", 2: "forecastex"},
    )
    by_state = {s.state: s for s in seats}
    assert set(by_state) == {"Ohio", "Wyoming"}
    assert by_state["Wyoming"].prob_r == pytest.approx(0.95)  # 1 - 0.05
    assert by_state["Wyoming"].source_party == "D"


def test_cross_venue_sides_can_come_from_different_venues():
    # R side only on polymarket, D side only on predictit → one seat whose
    # prob_r uses the R market, with both sides attributed.
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Maine Senate race in 2026?", 0.35),
            (2, "Which party will win the 2026 US Senate election in Maine? - Democratic", 0.63),
        ],
        venue_by_market={1: "polymarket", 2: "predictit"},
    )
    assert len(seats) == 1
    s = seats[0]
    assert s.prob_r == pytest.approx(0.35)
    assert s.r_market_id == 1
    assert s.d_market_id == 2


def test_no_venue_map_keeps_first_seen():
    # Without venue info every market ranks equal → first-seen wins, still
    # deterministic.
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Texas Senate race in 2026?", 0.78),
            (2, "Will the Republican party win the Texas general election for US Senate in 2026?", 0.74),
        ]
    )
    assert len(seats) == 1
    assert seats[0].market_id == 1
    assert seats[0].prob_r == pytest.approx(0.78)


def test_extraction_tier_fills_states_no_pattern_understands():
    # A phrasing no pattern knows ("Senate seat" instead of "Senate race") —
    # the audited LLM extraction supplies (party, state) and the seat exists.
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Ohio Senate race in 2026?", 0.62),
            (2, "GOP to take the Wyoming US Senate seat in the 2026 midterms", 0.93),
        ],
        extraction_by_market={2: ("R", "Wyoming")},
    )
    by_state = {s.state: s for s in seats}
    assert set(by_state) == {"Ohio", "Wyoming"}
    assert by_state["Wyoming"].prob_r == pytest.approx(0.93)
    assert by_state["Wyoming"].market_id == 2


def test_extraction_never_overrides_pattern_or_candidate():
    # Texas already has a pattern-parsed seat and Alaska a candidate seat —
    # extraction hints for the same states are ignored (lowest precedence).
    seats = extract_contested_seats(
        [
            (1, "Will the Republicans win the Texas Senate race in 2026?", 0.78),
            (2, "Texas Senate 2026 winner — Republican side", 0.50),
            (3, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
            (4, "Alaska 2026 Senate — GOP holds", 0.90),
        ],
        party_by_market={3: "R"},
        extraction_by_market={2: ("R", "Texas"), 4: ("R", "Alaska")},
    )
    by_state = {s.state: s for s in seats}
    assert by_state["Texas"].market_id == 1
    assert by_state["Texas"].prob_r == pytest.approx(0.78)
    assert by_state["Alaska"].market_id == 3
    assert by_state["Alaska"].prob_r == pytest.approx(0.375)


def test_extraction_d_side_flips_probability():
    seats = extract_contested_seats(
        [(9, "Dems take the Vermont US Senate seat in 2026", 0.85)],
        extraction_by_market={9: ("D", "Vermont")},
    )
    assert len(seats) == 1
    assert seats[0].prob_r == pytest.approx(0.15)
    assert seats[0].source_party == "D"


def test_candidate_markets_respect_venue_priority():
    # Same candidate priced on polymarket and forecastex → polymarket wins
    # even at a lower price (representative is venue-priority, not max-price,
    # across venues; max-price only breaks ties within one venue).
    seats = extract_contested_seats(
        [
            (1, "Will Dan Sullivan win the Alaska general election for US Senate in 2026?", 0.40),
            (2, "Will Dan Sullivan win the Alaska Senate race in 2026?", 0.375),
        ],
        party_by_market={1: "R", 2: "R"},
        venue_by_market={1: "forecastex", 2: "polymarket"},
    )
    assert len(seats) == 1
    assert seats[0].market_id == 2
    assert seats[0].prob_r == pytest.approx(0.375)


# --------------------------------------------------------------------------
# 2026-10-12 — LLM-named candidate legs, curated party overrides
# --------------------------------------------------------------------------

from pmi_core.engine.seat_mapping import (  # noqa: E402
    classify_markets,
    reduce_races,
)


def _none(_title):
    return None


def test_llm_candidate_extraction_joins_candidates_tier() -> None:
    markets = [
        (1, "California-11 House Winner - Scott Wiener", 0.81),
        (2, "California-11 House Winner - Connie Chan", 0.23),
    ]
    obs = classify_markets(
        markets, parse_direct=_none, parse_candidate=_none, key_code=lambda k: k,
        extraction_by_market={1: ("D", "California-11"), 2: ("D", "California-11")},
        extraction_kinds={1: "candidate", 2: "candidate"},
    )
    assert [o.tier for o in obs] == ["candidates", "candidates"]
    assert [o.label for o in obs] == ["Scott Wiener", "Connie Chan"]
    assert [o.resolved_via for o in obs] == ["llm_fallback", "llm_fallback"]
    # Same party, field spanned → no Republican path, not "1 − each candidate".
    r = reduce_races(obs, {1: "gemini", 2: "gemini"}, {})
    assert r["California-11"].p_r == pytest.approx(0.0)


def test_llm_party_direct_extraction_stays_directed() -> None:
    obs = classify_markets(
        [(1, "Who will win the Texas Senate? - Republican party", 0.9)],
        parse_direct=_none, parse_candidate=_none, key_code=lambda k: k,
        extraction_by_market={1: ("R", "Texas")}, extraction_kinds={1: "party_direct"},
    )
    assert obs[0].tier == "llm_fallback" and obs[0].resolved_via is None and obs[0].label is None


def test_llm_candidate_without_kind_map_stays_directed() -> None:
    # Callers that don't pass extraction_kinds get the pre-2026-10-12 behaviour.
    obs = classify_markets(
        [(1, "California-11 House Winner - Scott Wiener", 0.81)],
        parse_direct=_none, parse_candidate=_none, key_code=lambda k: k,
        extraction_by_market={1: ("D", "California-11")},
    )
    assert obs[0].tier == "llm_fallback"


def test_candidate_override_corrects_party_and_drops_independents() -> None:
    markets = [
        # forecastex candidate leg; the hint says D (the LLM's error) — override says R
        (1, "Will Justin Murphy win the New Jersey general election for US Senate in 2026?", 0.04),
        # polymarket candidate leg for an independent, hinted R by republican_on_yes
        (2, "Will Todd Achilles win the Idaho Senate race in 2026?", 0.04),
        # kalshi candidate-subject leg (no grammar) → llm_fallback candidate, hinted R
        (3, "Will Todd Achilless win the Senate race in Idaho?", 0.07),
    ]
    obs = classify_markets(
        markets,
        parse_direct=parse_seat_race,
        parse_candidate=parse_candidate_race,
        key_code=state_code,
        party_by_market={1: "D", 2: "R"},
        extraction_by_market={3: ("R", "Idaho")},
        extraction_kinds={3: "candidate"},
        office="senate",
    )
    assert [(o.market_id, o.party, o.tier) for o in obs] == [(1, "R", "candidates")]
