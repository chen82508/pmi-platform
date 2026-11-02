"""Unit tests for engine/race_calendar.has_race + the cycle walkers."""

from __future__ import annotations

from pmi_core.engine.race_calendar import (
    governor_states_for_cycle,
    has_race,
    senate_states_for_cycle,
)


def test_house_always_has_race():
    # All 435 districts are up every even year — no calendar lookup.
    assert has_race("OH", "house", 2026) is True
    assert has_race("WY", "house", 2030) is True


def test_senate_uses_calendar():
    # NC + TX hold a 2026 senate election. OH + FL do too, but only via 2026
    # SPECIAL elections (Vance / Rubio vacated their seats) — their regular-cycle
    # seats are up 2028/2030. See pmi_core/config/race_calendar.yaml, which the
    # us-senate-2026 index def relies on (35 ballot seats = 33 Class II + OH/FL
    # specials).
    assert has_race("NC", "senate", 2026) is True
    assert has_race("TX", "senate", 2026) is True
    assert has_race("OH", "senate", 2026) is True  # 2026 special (Vance seat)
    assert has_race("FL", "senate", 2026) is True  # 2026 special (Rubio seat)
    # OH's regular-cycle seats are also up in 2028 / 2030.
    assert has_race("OH", "senate", 2028) is True
    assert has_race("OH", "senate", 2030) is True


def test_senate_states_for_cycle_membership():
    states_2026 = senate_states_for_cycle(2026)
    assert "NC" in states_2026
    assert "TX" in states_2026
    # OH + FL appear via their 2026 special elections (Vance / Rubio seats).
    assert "OH" in states_2026
    assert "FL" in states_2026


def test_governor_period_walk():
    # 4-year states land on their `next` cycle.
    assert "GA" in governor_states_for_cycle(2026)  # next 2026
    # Odd-year governors land on their own off-year cycle, not even years.
    assert "VA" in governor_states_for_cycle(2029)
    assert "VA" not in governor_states_for_cycle(2026)
    # NH / VT are 2-year terms → up every even year.
    assert "NH" in governor_states_for_cycle(2026)
    assert "NH" in governor_states_for_cycle(2028)


def test_unknown_office_has_no_race():
    assert has_race("OH", "mayor", 2026) is False
