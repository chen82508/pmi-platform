"""Race identity — decide whether a market is a "who wins THIS seat" bet.

The MAGA / balance-of-power indexes model one number per race: P(Republican wins
the seat). Only GENERAL-election, seat-winner markets belong. But the ingested
venues list a lot of adjacent political markets that share the seat's keywords
yet answer a DIFFERENT question — and the old title grammar let several through,
so a district's lean averaged in primaries, nominations, county splits and
margin bets:

    primary   "Will Graham Platner win the Maine Democratic primary ... Senate?"
    nominee   "Will Ilhan Omar be the Democratic nominee for Senate in Minnesota?"
    county    "Will Haley Stevens win Genesee County in the Michigan Senate ...?"
    margin    "... Georgia Republican Governor Primary Runoff by less than 5%?"
    vote-share "Will Cory Booker receive at least 54% of the popular vote in the
               2026 New Jersey Senate election?"  (a share ladder, not a winner)
    state-leg "Will Scott Timko win the Pennsylvania House of Representatives
               District 12 seat?"   (state legislature, not the US House)

``exclusion_reason`` names why a market is NOT a clean seat-winner reading (or
None if it is), from a title (and optionally the ticker). It is venue-agnostic:
the reject is a property of the QUESTION, not who lists it, so it holds for every
venue including manifold's free-form user markets.

This module owns only the reject rules and cycle re-export; the office / state /
district / party grammar stays in ``extraction`` (reused, not duplicated). The
full ``resolve_race`` composition — office + cycle + reject + the reused grammar
— is assembled where the classifier is rewritten.

Pure-Python, no DB / network. Nothing imports this yet.
"""

from __future__ import annotations

import re

from pmi_core.engine.election_cycle import election_cycle

__all__ = ["election_cycle", "exclusion_reason"]

# A nominating contest, not the general: "... primary ...", "... runoff ...".
# Word-boundary so "primarily" / "run-off-the-mill" style noise can't trip it
# (there are none today, but the boundary keeps it honest).
_PRIMARY_RE = re.compile(r"\b(primary|primaries|runoff|run-off)\b", re.IGNORECASE)

# A party's nomination, not the seat: "be the Democratic nominee", "win the
# Republican nomination". "nominee"/"nomination" only ever mean the pre-general
# contest here.
_NOMINEE_RE = re.compile(r"\bnominee|nomination\b", re.IGNORECASE)

# A sub-geographic split, not the seat: "... win Genesee County ...". A real
# seat market never scopes to a single county.
_COUNTY_RE = re.compile(r"\bcounty\b", re.IGNORECASE)

# A margin / spread bet, not a winner: "by less than 5%", "by over 10 points",
# "margin of victory", "win by 5". The "by <comparator>" and "win by <number>"
# shapes avoid tripping on "by-election" (a special election, which IS a seat
# winner — it has no comparator/number after "by").
_MARGIN_RE = re.compile(
    r"\bby (?:less|more|over|under|at least|fewer)\b"
    r"|\bmargin of victory\b"
    r"|\bwin by \d"
    # "win Texas Senate by 11% or more" (manifold) — a spread, not a winner.
    r"|\bby \d+(?:\.\d+)?\s?(?:%|percent\b|points?\b|pts\b)",
    re.IGNORECASE,
)

# A vote-SHARE ladder, not a winner: "Will Cory Booker receive at least 54% of
# the popular vote in the 2026 New Jersey Senate election?" (kalshi
# KXVOTEGENERAL-*), "...% of the vote", "vote share", turnout / total-vote-count
# brackets. The LLM extraction tier read 195 of these as party-direct winner legs
# (CORR-1.11): a 0.00-priced "≥54%" rung for the favourite then entered the
# venue mean as "that party loses" and dragged every ladder state toward the
# other party (Kansas 42 vs 63 on every venue; New Jersey "flip to Rep").
_VOTE_SHARE_RE = re.compile(
    r"\breceive at least \d"
    r"|\b(?:of|share of) the (?:popular |total |statewide |national )?vote\b"
    r"|\bpopular vote\b"
    r"|\bvote share\b"
    r"|\bturnout\b"
    r"|\btotal vote count\b"
    r"|\bvote count (?:above|below|over|under|of at least)\b",
    re.IGNORECASE,
)

# A polling / forecast-comparison bet, not the seat: "outperform the Real Clear
# Polling average", "lead in the final poll". Resolves on polling, not the vote.
_POLLING_RE = re.compile(
    r"\bpolling average\b|\breal ?clear\b|\b538\b|\bfinal polls?\b|\bpoll(?:ing)? lead\b",
    re.IGNORECASE,
)

# A STATE legislature seat, not the US Congress. State forms read "<State> House
# of Representatives / State Senate / State Assembly / General Assembly"; the
# federal forecastex form always says "US House of Representatives", so the
# presence of "house of representatives" WITHOUT a federal marker is the tell.
_STATE_BODY_RE = re.compile(
    r"\bstate (?:house|senate|assembly|legislature)\b"
    r"|\bgeneral assembly\b"
    r"|\bhouse of delegates\b",
    re.IGNORECASE,
)
_FEDERAL_HOUSE_RE = re.compile(r"\bu\.?\s?s\.? house\b", re.IGNORECASE)
_HOUSE_OF_REPS_RE = re.compile(r"\bhouse of representatives\b", re.IGNORECASE)

# A combined / joint-outcome bet spanning MORE THAN ONE race — a sweep, a
# balance-of-power / chamber-control market, or a "win both offices" ticket
# ("Will Democrats sweep the Georgia Senate and Governor elections?", "Ohio
# 2026 Governor + Senate Bets — Republicans win both Governor and Senate").
# These share one race's keywords but resolve on several seats at once, so they
# are NOT a per-seat P(R) reading — and the LLM extraction tier can misread them
# as a single seat (observed: 2 phantom senate tags). Deterministic backstop.
# The "<office> and <office>" adjacency is the tell; a genuine single race that
# merely names a candidate's other office ("the Governor wins the X Senate race")
# has no such adjacency, so it is left alone.
_OFFICE_WORD = r"(?:senate|senator|governor|gubernatorial|governorship|house)"
_PARTY_WIN = r"(?:republicans?|democrats?|democratics?|gop|the (?:republican|democratic) party)"
_COMBO_RE = re.compile(
    r"\bsweep\b"
    r"|\bbalance of power\b"
    r"|\bwins? both\b"
    r"|\bat least one of\b"
    rf"|\b{_OFFICE_WORD} (?:and|&|\+) {_OFFICE_WORD}\b"
    # Two party-win clauses in one title span two races: "Will Democrats win the
    # Texas and Maine Senate seats, and Republicans win Michigan?" (polymarket).
    rf"|\b{_PARTY_WIN} win\b.+\b(?:and|&) {_PARTY_WIN} win\b"
    # Plural "seats" is never one race ("the Texas and Maine Senate seats").
    r"|\b(?:senate|house) seats\b",
    re.IGNORECASE,
)


def _is_state_legislature(title: str) -> bool:
    if _STATE_BODY_RE.search(title):
        return True
    # "<State> House of Representatives District N" (state lower chamber) —
    # federal titles carry "US House of Representatives", so a bare
    # "House of Representatives" with no federal marker is a state body.
    if _HOUSE_OF_REPS_RE.search(title) and not _FEDERAL_HOUSE_RE.search(title):
        return True
    return False


def exclusion_reason(title: str | None, external_id: str | None = None) -> str | None:
    """Why ``title`` is NOT a clean general-election seat-winner market, else None.

    Reasons (stable strings, usable as audit labels): ``"primary"``,
    ``"nominee"``, ``"sub_geographic"``, ``"margin"``, ``"vote_share"``,
    ``"polling"``, ``"state_legislature"``, ``"combo"``. Order matters only for
    which reason is reported first; any non-None means "exclude".
    ``external_id`` is accepted for future ticker-based rules (e.g. forecastex
    H/I primary prefixes) but title is the venue-agnostic signal.
    """
    if not title:
        return None
    if _PRIMARY_RE.search(title):
        return "primary"
    if _NOMINEE_RE.search(title):
        return "nominee"
    if _COUNTY_RE.search(title):
        return "sub_geographic"
    if _MARGIN_RE.search(title):
        return "margin"
    if _VOTE_SHARE_RE.search(title):
        return "vote_share"
    if _POLLING_RE.search(title):
        return "polling"
    if _is_state_legislature(title):
        return "state_legislature"
    if _COMBO_RE.search(title):
        return "combo"
    return None
