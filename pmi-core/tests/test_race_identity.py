"""Unit tests for race_identity.exclusion_reason — the seat-winner reject rules.

Titles are real (or faithfully shaped) rows from the live DB across venues, so
the rules are pinned to the noise the classifier actually sees.
"""

from __future__ import annotations

import pytest

from pmi_core.engine.race_identity import exclusion_reason

# --------------------------------------------------------------------------
# EXCLUDE — not a clean general-election seat-winner market
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,reason",
    [
        # primaries / runoffs (forecastex + polymarket, all offices)
        ("Will Graham Platner win the Maine Democratic primary election for US Senate in 2026?", "primary"),
        ("Will Briscoe Cain win the Texas 9th District Republican primary election for the US House of Representatives in 2026?", "primary"),
        ("Will Larry Rhoden win the South Dakota Republican primary for governor in 2026?", "primary"),
        ("Will Burt Jones win the Georgia Republican Governor Primary Runoff by less than 5%?", "primary"),
        # nominations
        ("Will Ilhan Omar be the Democratic nominee for Senate in Minnesota?", "nominee"),
        ("Will Tom Weiler be the Republican nominee for Senate in Minnesota?", "nominee"),
        # sub-geographic
        ("Will Haley Stevens win Genesee County in the Michigan Senate race?", "sub_geographic"),
        # margin / spread
        ("Will Republicans win the Texas governor race by less than 5%?", "margin"),
        ("Will the GOP win Ohio Senate by over 10 points?", "margin"),
        # state legislature (not US Congress)
        ("Will Scott Timko win the special election to fill the Pennsylvania House of Representatives District 12 seat?", "state_legislature"),
        ("Will Republicans win the Virginia House of Delegates in 2025?", "state_legislature"),
        ("Will the GOP control the Georgia State Senate?", "state_legislature"),
        # combo / joint-outcome — spans more than one race (the 2 live phantom tags)
        ("Will Democrats sweep the Georgia Senate and Governor elections?", "combo"),
        ("Will Republicans sweep the Texas Senate and Governor elections?", "combo"),
        ("Ohio 2026 Governor + Senate Bets — Republicans win both Governor and Senate", "combo"),
        ("2026 Midterms: Congress Balance of Power? — R-Senate, R-House", "combo"),
        ("Will Republicans win at least one of Governor and Senate in Ohio?", "combo"),
    ],
)
def test_excluded(title, reason):
    assert exclusion_reason(title) == reason


# --------------------------------------------------------------------------
# KEEP — genuine general-election seat-winner markets (None = not excluded)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        # forecastex federal House (has "US House of Representatives")
        "Will the Republican party win the California's 20th District general election for the US House of Representatives in 2026?",
        "Will Daniel Crenshaw win the Texas 2nd District general election for the US House of Representatives in 2026?",
        # kalshi party-direct (no "house of representatives" phrase at all)
        "Will Republican win the House race for AZ-04? - Republican party",
        "Will Republicans win the Senate race in California?",
        "Will the Republican party win the governorship in Arizona",
        # polymarket / forecastex governor + senate general
        "Will the Republicans win the Texas gubernatorial race in 2026?",
        "Will Shenna Bellows win the Maine general election for Governor in 2026?",
        # a single race that merely names a candidate's OTHER office — NOT a combo
        # (no "<office> and <office>" adjacency, no sweep/both). Governor running
        # for Senate is a genuine single Senate seat.
        "Will Governor Josh Green win the Hawaii Senate race in 2026?",
    ],
)
def test_kept(title):
    assert exclusion_reason(title) is None


# --------------------------------------------------------------------------
# edge cases
# --------------------------------------------------------------------------

def test_by_election_is_not_margin():
    # A by-election (special) is a seat winner — "by" with no comparator/number.
    assert exclusion_reason("Will the Republican win the AZ-07 by-election?") is None


def test_empty_title():
    assert exclusion_reason(None) is None
    assert exclusion_reason("") is None


def test_primary_precedes_margin_when_both_present():
    # A "primary ... by less than 5%" is reported as primary (checked first);
    # either way it's excluded, the label is just the first matched reason.
    assert exclusion_reason("Georgia Governor Primary Runoff by less than 5%") == "primary"


# --------------------------------------------------------------------------
# 2026-10-12 (CORR-1.11) — vote-share ladders, polling bets, multi-race combos
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,reason",
    [
        # kalshi KXVOTEGENERAL-* share ladders — the 195 llm_fallback mis-tags
        ("Will Cory Booker receive at least 54% of the popular vote in the 2026 New Jersey Senate election?", "vote_share"),
        ("Will Roger Marshall receive at least 52% of the popular vote in the 2026 Kansas Senate election?", "vote_share"),
        ("Will Scott Wiener receive at least 50% of the popular vote in the 2026 CA-11 House election?", "vote_share"),
        ("Will Jim Risch receive at least 50% of the popular vote in the 2026 Idaho Senate election?", "vote_share"),
        ("Will Republicans win 55% of the vote in the Ohio Senate race?", "vote_share"),
        ("Will turnout in the 2026 Georgia Senate election exceed 60%?", "vote_share"),
        # polling / forecast comparison
        ("Will James Talarico outperform the Real Clear Polling average in the 2026 U.S. Senate election in Texas? - Yes", "polling"),
        # manifold spread phrasing
        ("Will the Democrats win Texas Senate election by 11% or more?", "margin"),
        # two party-win clauses / plural seats = several races at once
        ("Will Democrats win the Texas and Maine Senate seats, and Republicans win Michigan?", "combo"),
        ("Will Republicans win the Texas Senate seat, and Democrats win Michigan and Maine?", "combo"),
    ],
)
def test_excluded_vote_share_polling_combo(title, reason):
    assert exclusion_reason(title) == reason


@pytest.mark.parametrize(
    "title",
    [
        "Will Republicans win the Senate race in New Jersey? - Justin Murphy",
        "Will Democratics win the Senate race in Texas? - James Talarico",
        "Which party will win the 2026 US Senate election in Texas? - Democrats (Talarico)",
        "Will the Republican Party win the CA-01 House seat?",
        "Who will win the CA-11 House election? - Connie Chan",
        "Texas US Senate Winner - Ken Paxton",
    ],
)
def test_kept_winner_legs_still_pass(title):
    assert exclusion_reason(title) is None
