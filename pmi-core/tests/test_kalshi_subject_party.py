"""kalshi party-aggregate legs annotated with a NOMINEE instead of "- party".

kalshi lists a district/seat's party-aggregate leg two ways:
  * "...? - Republican party"  (party in the tail)  — already parsed
  * "...? - Marty Jackley"     (party in the SUBJECT, nominee in the tail)

Only the first was recognised, so a district/state whose only kalshi legs used
the nominee form (e.g. SD-AL) was dropped. These tests pin the subject-party
reading and guard that candidate-SUBJECT legs still fall through.
"""

from __future__ import annotations

from pmi_core.engine.extraction import parse_district_race
from pmi_core.engine.seat_mapping import parse_seat_race

# --------------------------------------------------------------------------
# senate (parse_seat_race)
# --------------------------------------------------------------------------

def test_senate_party_subject_nominee_tail() -> None:
    assert parse_seat_race(
        "Will Republicans win the Senate race in Minnesota? - Michele Tafoya"
    ) == ("R", "Minnesota")
    assert parse_seat_race(
        "Will Democratics win the Senate race in Michigan? - Abdul El-Sayed"
    ) == ("D", "Michigan")


def test_senate_party_tail_still_authoritative() -> None:
    assert parse_seat_race(
        "Will Democratics win the Senate race in Connecticut? - Democratic party"
    ) == ("D", "Connecticut")


def test_senate_candidate_subject_not_party_direct() -> None:
    # A candidate-subject leg must NOT be read as party-direct (falls through to
    # the candidate tier, where party resolves from the name/hint).
    assert parse_seat_race("Will John Fetterman win the Senate race in Pennsylvania?") is None
    assert parse_seat_race("Will Todd Achilless win the Senate race in Idaho?") is None


# --------------------------------------------------------------------------
# house (parse_district_race)
# --------------------------------------------------------------------------

def test_house_party_subject_nominee_tail_sdal() -> None:
    # The reported miss: SD-AL, only nominee-annotated legs. AL canonicalises to
    # district 1 → "South Dakota-1".
    assert parse_district_race(
        "Will Republican win the House race for SD-AL? - Marty Jackley"
    ) == ("R", "South Dakota-1")
    assert parse_district_race(
        "Will Democratic win the House race for WI-08? - Tony Wied"
    ) == ("D", "Wisconsin-8")


def test_house_a_party_subject() -> None:
    assert parse_district_race(
        "Will a Republican win the House race for CA-21? - Kyle Kirkland"
    ) == ("R", "California-21")


def test_house_party_tail_still_authoritative() -> None:
    assert parse_district_race(
        "Will Democratic win the House race for CA-40? - Democratic party"
    ) == ("D", "California-40")


def test_house_candidate_subject_not_party_direct() -> None:
    assert parse_district_race("Will Bill Hill win the House race for AK-AL?") is None
