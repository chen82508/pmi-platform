"""Pure-logic tests for climate_compute (the DB folds are verified live)."""
import re
from datetime import UTC, datetime
from types import SimpleNamespace

from pmi_core.engine.climate_compute import (
    _CITYHEAT_KW,
    _CITYHEAT_REJECT,
    _RANK_HOTTEST,
    _TITLE_RECORD_YEAR,
    _WARM_KW,
    _WARM_REJECT,
    _bucket_midpoint,
    _city_code,
    _cityheat_board,
    _fx_anomaly_leg,
    _kalshi_year,
    _kalshi_yearmonth,
    _pava_nonincreasing,
    _pick_month,
    _sum_vol,
    _survival_expected,
    _title_year,
    _warming_board,
)


def test_rank_hottest_excludes_ordinal_variants():
    for t in ["Will 2026 be the second-hottest year on record?",
              "Will 2026 be the fifth-hottest year on record?",
              "Will 2026 rank as the sixth-hottest year on record or lower"]:
        assert _RANK_HOTTEST.search(t), t
    for t in ["Will 2026 be the hottest year on record?", "Will 2026 be the warmest year on record?"]:
        assert not _RANK_HOTTEST.search(t), t


def test_sum_vol_ignores_none_and_returns_none_when_all_absent():
    pairs = [(None, {"volume_24h": 100.0}), (None, {"volume_24h": None}), (None, {"volume_24h": 20.0})]
    assert _sum_vol(pairs) == 120.0
    assert _sum_vol([(None, {"volume_24h": None})]) is None
    assert _sum_vol([]) is None


def _keeps(kw: str, reject: str, title: str) -> bool:
    """Mirror the SQL `title ~* kw AND title !~* reject` (POSIX ci) in Python."""
    return bool(re.search(kw, title, re.I)) and not re.search(reject, title, re.I)


def test_bucket_midpoint_decimal_range():
    assert _bucket_midpoint("1.04 to 1.09") == 1.065


def test_bucket_midpoint_integer_temp_range():
    assert _bucket_midpoint("101° to 102°") == 101.5


def test_bucket_midpoint_open_ended():
    assert _bucket_midpoint("109° or above") == 109.0
    assert _bucket_midpoint("100° or below") == 100.0  # single-number fallback


def test_bucket_midpoint_none():
    assert _bucket_midpoint("Hottest ever") is None
    assert _bucket_midpoint(None) is None


def test_city_code():
    assert _city_code("KXHIGHTNY") == "NY"
    assert _city_code("KXHIGHLAX") == "LAX"


# ── Non-kalshi capture filters (real sampled titles) ──────────────────────────
def test_warming_keyword_keeps_real_markets():
    for t in [
        "Will the Average Global Temperature in December 2026 be greater than 1.0 degrees Celsius above pre-industrial",
        "Will the Average US Temperature in August 2026 be greater than 75 degrees Fahrenheit?",
        "Will 2026 be the hottest year on record?",
        "In which year will global warming reach 1.5 degrees Celsius? — 2039",
        "Will August 2026 be the warmest August on record, according to Copernicus?",
    ]:
        assert _keeps(_WARM_KW, _WARM_REJECT, t), t


def test_warming_reject_kills_noise():
    for t in [
        "Will a room-temperature and ambient-pressure superconductor be used commercially",
        "Ragnarök Question Series: if a global climate disaster occurs by 2100",
        "If >100 offensive nuclear detonations occur in a single conflict by 2030, will this cause a global",
        "Will non-compliance with climate change policies be cited as a cause of a war",
        "Humanoid robot breaks human marathon record before 2027?",  # "marath-on record"
    ]:
        assert not _keeps(_WARM_KW, _WARM_REJECT, t), t


def test_cityheat_keyword_keeps_cross_venue_city_temps():
    # Real sampled IBKR Environmental titles — the display-layer capture that lets
    # non-kalshi city-temperature markets join City Heat (holdings / count).
    for t in [
        "New York City Daily Temperature High — Above 65",
        "Boston Daily Temperature Low — Below 59",
        "New York City Hourly Temperature — Above 67",
        "Highest Temperature in Austin — 100° to 101°",
    ]:
        assert _keeps(_CITYHEAT_KW, _CITYHEAT_REJECT, t), t


def test_cityheat_reject_keeps_global_anomaly_out():
    # A global-anomaly market belongs to Global Warming, not a city; body / sea
    # "temperature" is not city heat either.
    for t in [
        "Global Temperature — Above 1.2",
        "Will the Average Global Temperature in December 2026 be greater than 1.0",
        "Sea surface temperature anomaly exceeds 1°C",
        "Human body temperature record",
    ]:
        assert not _keeps(_CITYHEAT_KW, _CITYHEAT_REJECT, t), t


def test_record_year_folds_across_venues():
    assert _TITLE_RECORD_YEAR.search("Will 2026 be the hottest year on record?")
    assert _TITLE_RECORD_YEAR.search("hottest year on record")
    assert not _TITLE_RECORD_YEAR.search("Will the Average US Temperature exceed 75 F?")


# ── forecastex threshold ladder → E[°C] (same family as the BTC strike ladder) ──
def test_fx_anomaly_leg_parses_global_annual():
    assert _fx_anomaly_leg(
        "Will the average Global Temperature in 2026 be greater than 1.25 C (2.25 F) above the 20th century average?"
    ) == ("global", 2026, 1.25)
    assert _fx_anomaly_leg(
        "Will the average Global Temperature in 2027 be greater than 1.0 degrees Celsius above the 20th century average?"
    ) == ("global", 2027, 1.0)


def test_fx_anomaly_leg_rejects_non_annual_global():
    # monthly, cumulative "any year", and US (°F) ladders are a different quantity
    for t in [
        "Will the Average Global Temperature in August 2026 be greater than 1 degrees Celsius above the 20th century average?",
        "Will the Average Global Temperature for any year be greater than 1.5 degrees Celsius above preindustrial levels by the end of 2045?",
        "Will the average US Temperature in 2026 be greater than 54 F (12.2 C)?",
    ]:
        assert _fx_anomaly_leg(t) is None, t


# ── period parsing: keep a board to ONE year / ONE month ──────────────────────
def test_kalshi_year_parses_ticker():
    assert _kalshi_year("KXGTEMP-26") == 2026
    assert _kalshi_year("KXHMONTHRANGE-26SEP-B12") == 2026
    assert _kalshi_year("kxhmonth-27aug") == 2027
    assert _kalshi_year("KXHURRICANENAMES-26") is None  # not a warming series
    assert _kalshi_year(None) is None


def test_kalshi_yearmonth_parses_month():
    assert _kalshi_yearmonth("KXHMONTHRANGE-26SEP-B12") == (2026, 9)
    assert _kalshi_yearmonth("KXHMONTH-26AUG") == (2026, 8)
    assert _kalshi_yearmonth("KXGTEMP-26") is None  # annual, no month
    assert _kalshi_yearmonth("KXHMONTHRANGE-26ZZZ-B1") is None  # bad month token


def test_title_year_extracts_target_year():
    assert _title_year("Will 2027 be the warmest year?") == 2027
    assert _title_year("Will 2026 be the hottest year on record?") == 2026
    assert _title_year("hottest year on record") is None


def test_pick_month_folds_a_single_month():
    # Two months present → result is exactly one month's items (never a mix).
    items = [
        ("a", {}, (2026, 8)),
        ("b", {}, (2026, 8)),
        ("c", {}, (2026, 9)),
    ]
    picked = _pick_month(items)
    assert picked, "should pick something"
    assert len({t[-1] for t in picked}) == 1, picked  # exactly one (year, month)
    assert _pick_month([]) == []


def test_pava_enforces_non_increasing():
    out = _pava_nonincreasing([0.86, 0.90, 0.58, 0.41, 0.48])
    assert all(out[i] >= out[i + 1] for i in range(len(out) - 1)), out
    assert abs(sum(out) - (0.86 + 0.90 + 0.58 + 0.41 + 0.48)) < 1e-9  # PAVA preserves mass


def test_survival_expected_matches_hand_calc():
    ladder = [(1.10, 0.86), (1.15, 0.90), (1.20, 0.58), (1.25, 0.41), (1.30, 0.48),
              (1.33, 0.17), (1.38, 0.09), (1.40, 0.08), (1.45, 0.06), (1.50, 0.10)]
    assert _survival_expected(ladder) == 1.25  # real forecastex 2026 global ladder
    assert _survival_expected([(1.0, 0.9), (1.1, 0.5)]) is None  # <3 rungs → None


def _row(i, venue, title, ext, price, yes_sub=None, play=False):
    return SimpleNamespace(id=i, venue=venue, title=title, external_id=ext, last_price=price,
                           last_volume_24h=10.0, last_price_at=None, url=None,
                           yes_sub=yes_sub, is_play_money=play)


def test_warming_header_counts_the_cards_not_the_display_layer():
    # Header said 160 contracts over question cards summing to 50: it counted every
    # matched row, including ones that fold into no card.
    yy = datetime.now(UTC).year % 100
    rows = [
        _row(1, "kalshi", "Hottest year?", f"KXGTEMP-{yy}", 0.51),
        _row(2, "kalshi", "Hottest year?", f"KXGTEMP-{yy + 1}", 0.40),        # other year: no card
        _row(3, "manifold", f"Will 20{yy} be the hottest year on record?", "m-3", 0.60, play=True),
        _row(4, "polymarket", "Will July be hotter than June?", "p-4", 0.30),  # keyword hit, no card
    ]
    b = _warming_board(rows)
    assert b["component_count"] == sum(q["n_contracts"] for q in b["questions"].values()) == 2
    assert b["exchanges"] == ["kalshi", "manifold"]
    assert b["tracked_count"] == len(b["holdings"]) == 4


def test_cityheat_header_counts_the_cards_not_other_days():
    rows = [
        _row(1, "kalshi", "Austin high", "KXHIGHAUS-26OCT11-B95.5", 0.6, "95° to 96°"),
        _row(2, "kalshi", "Austin high", "KXHIGHAUS-26OCT11-B97.5", 0.4, "97° to 98°"),
        _row(3, "kalshi", "Austin high", "KXHIGHAUS-26OCT12-B80.5", 0.9, "80° to 81°"),  # cooler day: no card
        _row(4, "ibkr", "New York City Daily Temperature High — Above 65", "ibkr-4", 0.5),
    ]
    b = _cityheat_board(rows)
    assert b["component_count"] == sum(a["n_contracts"] for a in b["anchors"]) == 2
    assert b["exchanges"] == ["kalshi"]
    assert b["tracked_count"] == 4


def test_sum_vol_counts_real_money_only():
    # Manifold "volume" is mana, not dollars: the Hottest-year card read $39K of
    # which $27K was play money (2026-10-12).
    pairs = [
        (None, {"volume_24h": 10_632.0, "play_money": False}),
        (None, {"volume_24h": 27_293.0, "play_money": True}),
        (None, {"volume_24h": 1_166.0}),  # older members carry no flag → real
    ]
    assert _sum_vol(pairs) == 11_798.0
    assert _sum_vol([(None, {"volume_24h": 27_293.0, "play_money": True})]) is None
