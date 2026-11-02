"""Unit tests for the pure GDP-expectation board math (pmi_api.gdp_board).

No DB / fixtures — the grouping, E[growth] reading and sentiment mapping are all
pure functions, so we feed synthetic Member tuples and assert analytic values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmi_core.engine import gdp_board as gb


def test_is_us_accepts_us_and_country_less_kalshi():
    assert gb.is_us("Will US GDP growth in 2026 be between 2.0% and 2.5%?")
    assert gb.is_us("GDP growth in 2026? - 1.6% to 2.0%")  # kalshi, country-less
    assert gb.is_us("Will the annualized growth rate in US Real GDP exceed 1% in Q2 2026?")


def test_is_us_rejects_other_countries_and_junk():
    assert not gb.is_us("Will China's 2026 annual GDP growth (Y/Y) be 9% or higher?")
    assert not gb.is_us("Will Eurozone GDP growth in Q2 2026 be between 0.0% and 0.3%?")
    # presidency × GDP combo and level-ratio junk are dropped even though "US"/"GDP" appear
    assert not gb.is_us("Who will win the presidency in 2028 and will average annual Real GDP growth...")
    assert not gb.is_us("Will US real GDP in Q1 2030 be at least 50% greater than in Q1 2025?")


def test_is_us_rejects_inflected_country_names():
    """The exclusion terms are stems, so they must catch the inflected forms.

    Written as `\\bgerman\\b` the stems could not match their own countries, and
    because "real gdp" is itself a positive US signal these titles passed the
    whole chain and were folded into the US board (27 forecastex German
    contracts were one poll away from doing exactly that).
    """
    assert not gb.is_us("Will Germany's quarterly change in real GDP exceed 0.5% in Q2 2026?")
    assert not gb.is_us("Will Italy's real GDP growth exceed 1% in 2026?")
    assert not gb.is_us("Will Italian real GDP growth exceed 1% in 2026?")
    assert not gb.is_us("Will Japanese real GDP growth exceed 1% in 2026?")
    assert not gb.is_us("Will Chinese official real GDP growth be revised downward in 2026?")
    assert not gb.is_us("Will Korean real GDP growth exceed 2% in 2026?")
    assert not gb.is_us("Will Brazilian real GDP growth exceed 2% in 2026?")
    assert not gb.is_us("Will Indian real GDP growth exceed 6% in 2026?")


def test_is_us_stems_do_not_swallow_unrelated_words():
    """`uk` and `india` keep both boundaries so they cannot eat English words.

    Open-suffix stems are the right default, but these two collide: `uk\\w*`
    matches "Ukraine", `india\\w*` matches "Indiana".
    """
    assert gb.is_us("Will US real GDP growth exceed 2% in 2026?")
    assert gb.is_us("Will real GDP growth in Indiana exceed 2% in 2026?")


def test_is_us_rejects_a_possessive_subject_that_is_not_the_us():
    """Sub-national and unlisted foreign economies, caught by title shape.

    `_OTHER` can only reject geographies someone enumerated, so it fails open.
    This catches the shape instead — which is what covers the 549 forecastex
    US-STATE contracts that are out of scope for a national index and are
    excluded today only because they happen not to say "real GDP".
    """
    assert not gb.is_us("Will Texas's quarterly GDP growth exceed 0% in Q1 2027?")
    assert not gb.is_us("Will California's quarterly change in real GDP exceed 2% in Q2 2026?")
    assert not gb.is_us("Will Slovenia's real GDP growth exceed 1% in 2026?")  # unlisted country
    # The US itself is exempt, and a title with no possessive subject is untouched.
    assert gb.is_us("Will the US's real GDP growth exceed 2% in 2026?")
    assert gb.is_us("Will US GDP growth in 2026 be between 2.0% and 2.5%?")


def test_ibkr_gdp_titles_are_all_excluded_by_the_existing_method():
    """IBKR (ForecastTrader) carries only US-STATE and FOREIGN GDP markets — no
    US-national one. Onboarding IBKR as a source therefore adds ZERO members to
    the national GDP board, and that is correct: the same title-prefilter + is_us
    the board already runs excludes every one (no IBKR-specific handling). A
    market is a member only if title_prefilter AND is_us both pass."""
    def member(t: str) -> bool:
        return gb.title_prefilter(t) and gb.is_us(t)

    # US-state and bare foreign GDP: no "growth"/"real gdp" → title_prefilter drops them.
    for t in ["Pennsylvania GDP — Above 3.0%", "Washington GDP — Above 4.0%",
              "California GDP — Above 2.0%", "Florida GDP — Above 0.0%",
              "Germany GDP — Above 1.0%", "Japan GDP — Above 0.0%"]:
        assert not member(t), t
    # Foreign "Real GDP": clears the prefilter but is_us rejects the country.
    for t in ["Canada Real GDP — Above 0.0%", "Eurozone Real GDP — Above 1.0%",
              "Hong Kong Real GDP — Above 2.1%"]:
        assert not member(t), t


def test_anchor_of_quarter_and_annual():
    assert gb.anchor_of("US GDP growth in Q2 2026 ...")[:2] == ("quarter", "2026Q2")
    assert gb.anchor_of("GDP growth in 2027? - 2.1% to 2.5%")[:2] == ("annual", "2027")
    assert gb.anchor_of("no period here") is None


def test_parse_reading_threshold_bucket_and_sanity_gate():
    assert gb.parse_reading("... exceed 1.5% in Q2 2026?") == ("thr", 1.5)
    assert gb.parse_reading("GDP growth in 2026? - 1.6% to 2.0%") == ("bkt", 1.8)
    assert gb.parse_reading("GDP growth in 2026? - 0.0% or Below")[0] == "bkt"
    # a year mis-read as a growth value is rejected by the ±20pp gate
    assert gb.parse_reading("... 2029 ...") is None


def test_parse_reading_only_calls_it_negative_when_the_title_says_so():
    """No percentage and no direction word is unreadable, not a -0.5% bucket."""
    assert gb.parse_reading("Will US real GDP growth beat expectations in 2026?") is None
    assert gb.parse_reading("Will the US experience negative GDP growth in 2026?") == ("bkt", -0.5)
    assert gb.parse_reading("Will the US economy contract in 2026?") == ("bkt", -0.5)


def test_parse_reading_ignores_year_reads_only_pct_number():
    # A four-digit year in the title must not be read as a growth value —
    # only the %-attached number counts (the _NUM `\d+ %` fix).
    assert gb.parse_reading("Will US GDP grow by less than 3.5% in 2026?") == ("bkt", 3.0)
    assert gb.parse_reading("Will US real GDP growth be above 2% in 2027?") == ("bkt", 2.5)
    # "exceed" ladder likewise ignores the trailing year
    assert gb.parse_reading("Will US Real GDP exceed 1% in 2030?") == ("thr", 1.0)


def test_parse_reading_kalshi_quarterly_ladder_is_a_threshold():
    """"increase by more than x% - Above x%" is P(growth > x), at x, not x+0.5."""
    t = "Will **real GDP** increase by more than 4.0% in Q4 2026? - Above 4.0%"
    assert gb.parse_reading(t) == ("thr", 4.0)
    # Kalshi's annual "x% or Above" tops a bucket set and stays a bucket.
    assert gb.parse_reading("GDP growth in 2026? - 3.0% or Above") == ("bkt", 3.5)


def test_isotonic_fit_outvotes_a_lone_print_and_splits_a_tie():
    assert gb._isotonic_nonincreasing([0.9, 0.7, 0.5]) == [0.9, 0.7, 0.5]
    # One 1¢ print against two neighbours that agree: the neighbours win.
    assert gb._isotonic_nonincreasing([0.87, 0.01, 0.59, 0.55]) == [0.87, 0.55, 0.55, 0.55]
    # One against one: no majority, so the pair meets in the middle.
    assert gb._isotonic_nonincreasing([0.87, 0.01, 0.59]) == [0.87, 0.30, 0.30]


def test_a_lone_low_print_does_not_collapse_the_ladder():
    """ForecastEx Q4 2026: a 10-contract fill at 1¢ on the 0.5% strike.

    The running-minimum fit copied 0.01 onto every higher strike and the venue
    read -0.47%; its other seven strikes put E near +0.7%.
    """
    ladder = [(-4.5, 0.94), (-3.5, 0.91), (-2.5, 0.90), (-1.5, 0.89),
              (-0.5, 0.87), (0.5, 0.01), (1.0, 0.59), (1.5, 0.55)]
    e = gb._e_growth([("thr", x, p) for x, p in ladder])
    assert 0.5 < e < 0.8


def test_kalshi_quarterly_ladder_reads_as_a_survival_function():
    """Q3 2026: read as buckets this ladder gave 2.0%; it crosses 50% near 3%."""
    prices = {0.0: 0.99, 0.5: 0.99, 1.0: 0.99, 1.5: 0.97, 2.0: 0.92,
              2.5: 0.80, 3.0: 0.64, 3.5: 0.30, 4.0: 0.08}
    members = [
        gb.Member(i, "kalshi",
                  f"Will **real GDP** increase by more than {x:.1f}% in Q3 2026? - Above {x:.1f}%",
                  p, 1_000.0)
        for i, (x, p) in enumerate(prices.items())
    ]
    a = gb.build_board(members, min_year=2026)["anchors"][0]
    assert a["period"] == "2026Q3"
    assert 3.0 <= a["e_growth"] <= 3.2


def test_tiny_volumes_do_not_decide_the_venue_fold():
    """10 contracts on one venue vs 0 on the other is no reason to trust it 10:1."""
    strikes = (1, 2, 3)
    low, high = (0.5, 0.3, 0.1), (0.9, 0.7, 0.5)
    members = [
        *(gb.Member(i, "forecastex", f"Will US Real GDP exceed {x}% in Q4 2026?", p, v)
          for i, (x, p, v) in enumerate(zip(strikes, low, (10.0, 0.0, 0.0), strict=False))),
        *(gb.Member(10 + i, "kalshi", f"Will US Real GDP exceed {x}% in Q4 2026?", p, 0.0)
          for i, (x, p) in enumerate(zip(strikes, high, strict=False))),
    ]
    e_low = gb._e_growth([("thr", x, p) for x, p in zip(strikes, low, strict=False)])
    e_high = gb._e_growth([("thr", x, p) for x, p in zip(strikes, high, strict=False)])
    a = gb.build_board(members, min_year=2026)["anchors"][0]
    assert a["e_growth"] == round((e_low + e_high) / 2, 2)


def test_sentiment_mapping_calibration():
    assert gb.sentiment(2.0) == 50.0          # neutral baseline → 50 exactly
    assert round(gb.sentiment(3.0)) == 70     # +1pp above potential → 70
    assert round(gb.sentiment(1.0)) == 30     # symmetric
    assert gb.sentiment(None) is None


def test_build_board_folds_a_bucket_anchor_across_venues():
    # Two venues, one clean annual bucket PMF each, both implying ~2.0% → ~50.
    members = [
        gb.Member(1, "kalshi", "GDP growth in 2026? - 1.6% to 2.0%", 0.5, 100.0),
        gb.Member(2, "kalshi", "GDP growth in 2026? - 2.1% to 2.5%", 0.5, 100.0),
        gb.Member(3, "polymarket", "Will US GDP growth in 2026 be between 1.5% and 2.0%?", 0.5, 100.0),
        gb.Member(4, "polymarket", "Will US GDP growth in 2026 be between 2.0% and 2.5%?", 0.5, 100.0),
        gb.Member(5, "china", "Will China's 2026 GDP growth be 9% or higher?", 0.5, 100.0),  # excluded
    ]
    board = gb.build_board(members, min_year=2026)
    assert board["n_anchors"] == 1
    a = board["anchors"][0]
    assert a["period"] == "2026" and a["horizon"] == "annual"
    assert 1.8 <= a["e_growth"] <= 2.2       # midpoints ~1.8–2.25 → ~2.0
    assert 45 <= board["score"] <= 55        # neutral-ish
    assert board["regime"] == "Neutral"


def test_anchor_counts_every_priced_venue_even_when_it_cant_read_e():
    # kalshi has a clean bucket PMF (reads E); manifold has only 2 thresholds,
    # so `_e_growth` returns None for it — but its priced contracts must still
    # show in `venues` + `n_contracts` (the "3 exchanges shown, 2 counted" fix).
    members = [
        gb.Member(1, "kalshi", "GDP growth in 2026? - 1.6% to 2.0%", 0.5, 100.0),
        gb.Member(2, "kalshi", "GDP growth in 2026? - 2.1% to 2.5%", 0.5, 100.0),
        gb.Member(3, "manifold", "Will US Real GDP exceed 3% in 2026?", 0.4, 1.0),
        gb.Member(4, "manifold", "Will US Real GDP exceed 4% in 2026?", 0.2, 1.0),
    ]
    board = gb.build_board(members, min_year=2026)
    a = board["anchors"][0]
    assert a["venues"] == ["kalshi", "manifold"]   # thin venue shown
    assert a["n_contracts"] == 4                    # all priced contracts counted
    # ...but only kalshi's bucket read moves E (manifold's 2 thresholds < 3).
    assert 1.8 <= a["e_growth"] <= 2.2


def test_anchor_of_refuses_a_title_spanning_several_quarters():
    # Only "Q3 2026" is year-attached, so the old regex filed this under Q3 and
    # read a three-quarter question as that one quarter's expectation.
    assert gb.anchor_of(
        "[ACX 2026] Will the United States experience negative GDP growth during Q1, Q2, or Q3 2026?"
    ) is None
    # ...while a title naming one quarter twice is still that quarter.
    assert gb.anchor_of("US GDP growth in Q2 2026 — will Q2 beat potential?")[:2] == (
        "quarter",
        "2026Q2",
    )


def test_a_lone_bucket_cannot_read_e_growth():
    """One bucket has no complement: Σx·p/Σp collapses to its midpoint."""
    assert gb._e_growth([("bkt", -0.5, 0.03)]) is None
    assert gb._e_growth([("bkt", -0.5, 0.97)]) is None
    # two buckets are a (partial) PMF and do read
    assert gb._e_growth([("bkt", 1.0, 0.5), ("bkt", 3.0, 0.5)]) == 2.0


def test_a_lone_bucket_does_not_outvote_a_threshold_ladder():
    """The Q3-2026 regression: one play-money bucket pinned E to -0.5%.

    ForecastEx prices P(growth > 2.5%) at 0.83, so the anchor must read clearly
    expansionary; a single "negative growth" bucket carrying ~9,000 mana against
    ~40 real dollars had been dragging it to -0.49% / sentiment 10.8.
    """
    members = [
        gb.Member(1, "manifold",
                  "Will the United States experience negative GDP growth in Q3 2026?", 0.173, 9085.0),
        gb.Member(2, "forecastex",
                  "Will the annualized growth rate in US Real GDP exceed 1% in Q3 2026?", 0.97, 10.0),
        gb.Member(3, "forecastex",
                  "Will the annualized growth rate in US Real GDP exceed 1.5% in Q3 2026?", 0.94, 20.0),
        gb.Member(4, "forecastex",
                  "Will the annualized growth rate in US Real GDP exceed 2.5% in Q3 2026?", 0.83, 10.0),
    ]
    a = gb.build_board(members, min_year=2026)["anchors"][0]
    assert a["e_growth"] > 2.0
    assert a["sentiment"] > 55
    assert a["venues"] == ["forecastex", "manifold"]  # still shown, just silent
    assert a["n_contracts"] == 4


def test_play_money_volume_never_outweighs_a_real_money_venue():
    """Mana is site credit, not dollars — it must not win the liquidity fold."""
    members = [
        # manifold: a readable 2-bucket PMF at ~0%, with huge mana "volume"
        gb.Member(1, "manifold", "Will US GDP growth in 2026 be between -1.0% and 0.0%?", 0.5, 50_000.0),
        gb.Member(2, "manifold", "Will US GDP growth in 2026 be between 0.0% and 1.0%?", 0.5, 50_000.0),
        # kalshi: a readable PMF at ~2%, with real but small volume
        gb.Member(3, "kalshi", "GDP growth in 2026? - 1.6% to 2.0%", 0.5, 100.0),
        gb.Member(4, "kalshi", "GDP growth in 2026? - 2.1% to 2.5%", 0.5, 100.0),
    ]
    a = gb.build_board(members, min_year=2026)["anchors"][0]
    assert 1.8 <= a["e_growth"] <= 2.2  # kalshi's real-money read carries it


def test_a_days_old_quote_does_not_join_a_live_ladder():
    """Q4 2026: a 69h-old strike crossed the live ones and was silently clamped.

    Fresh quotes said P(>1%) = 0.32 while a stale one still claimed
    P(>1.5%) = 0.46 — impossible for one distribution. The stale strike must
    leave the ladder, not be flattened onto its neighbour.
    """
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    fresh = [
        gb.Member(1, "forecastex", "Will US Real GDP exceed -2.5% in Q4 2026?", 0.75, 10.0, now),
        gb.Member(2, "forecastex", "Will US Real GDP exceed -1.5% in Q4 2026?", 0.65, 20.0, now),
        gb.Member(3, "forecastex", "Will US Real GDP exceed 1% in Q4 2026?", 0.32, 5.0, now),
    ]
    stale = gb.Member(
        4, "forecastex", "Will US Real GDP exceed 1.5% in Q4 2026?", 0.46, 0.0,
        now - timedelta(hours=69),
    )
    assert gb._contemporaneous([*fresh, stale]) == fresh
    # ...and the anchor reads off the three live strikes alone
    a = gb.build_board([*fresh, stale], min_year=2026)["anchors"][0]
    assert a["n_contracts"] == 3


def test_unstamped_quotes_are_kept():
    """No `price_at` (older ingest rows, tests) means nothing to judge staleness on."""
    members = [
        gb.Member(1, "kalshi", "GDP growth in 2026? - 1.6% to 2.0%", 0.5, 100.0),
        gb.Member(2, "kalshi", "GDP growth in 2026? - 2.1% to 2.5%", 0.5, 100.0),
    ]
    assert gb._contemporaneous(members) == members


def test_build_board_drops_periods_before_min_year():
    members = [
        # a lingering unresolved 2025 market must not become the headline
        gb.Member(1, "manifold", "Will US Real GDP growth in 2025 exceed 8%?", 0.9, 1.0),
        gb.Member(2, "manifold", "Will US Real GDP growth in 2025 exceed 9%?", 0.8, 1.0),
        gb.Member(3, "manifold", "Will US Real GDP growth in 2025 exceed 10%?", 0.7, 1.0),
        gb.Member(4, "kalshi", "GDP growth in 2026? - 1.6% to 2.0%", 0.5, 100.0),
        gb.Member(5, "kalshi", "GDP growth in 2026? - 2.1% to 2.5%", 0.5, 100.0),
    ]
    board = gb.build_board(members, min_year=2026)
    periods = [a["period"] for a in board["anchors"]]
    assert "2025" not in periods
    assert board["headline_period"] == "2026"
