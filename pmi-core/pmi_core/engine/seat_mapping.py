"""Market → contested-seat mapping for balance-of-power boards (CORR-1.3).

The senate-board's seat distribution (engine/seat_distribution.py) needs ONE
probability per contested seat. A keyword selector for "senate" is far too
broad — on live Polymarket data it matches ~390 markets: primary-nominee
markets ("Will X be the Democratic nominee for Senate in Y?"), foreign senates
(Peru / Brazil / Philippines), procedural markets ("Will the Senate pass a
reconciliation bill?"), Majority-Leader markets, and chamber-level brackets
("47 or fewer Senate seats"). Feeding all of those into a Poisson-binomial as
if each were an independent seat produces nonsense (E[R seats] > 100).

The clean per-seat signal is a small set of tight per-venue title patterns
(one phrasing per venue):

    polymarket: "Will the Republicans win the Ohio Senate race in 2026?"
    forecastex: "Will the Republican party win the Texas general election
                 for US Senate in 2026?"
                "Will Democratic Primary Winner win the Wyoming general
                 election for US Senate in 2026?"  (party-proxy: the primary
                 winner IS the nominee, so this is P(party wins the seat))
    predictit:  "Which party will win the 2026 US Senate election in
                 Maine? - Republican"  (one market row per outcome leg)
    kalshi:     "Will Democratics win the Senate race in Alabama? -
                 Democratic party"  (the party-aggregate outcome leg; the
                 party reads off the trailing leg, candidate legs are skipped)

This module parses (party, state) out of those titles, groups by state, and
collapses each state to a single P(Republican wins the seat):

  * if a "Republicans win" market exists  → p_r = its price
  * else if a "Democrats win" market exists → p_r = 1 - its price

States that have both (the markets are complementary outcomes of the same
seat) therefore collapse to ONE seat, not two — which is the whole point.

Cross-venue merge (CORR-1.3 follow-up, 2026-06-12): with the index declaring
several ``venues:``, one (state, party) side can have markets on multiple
venues. We do NOT average across venues — prob_r must stay tied to one real
market's price so delta_14d attribution against that market's snapshot
history keeps meaning. Instead the representative per side is picked by
venue priority (``VENUE_PRIORITY``: polymarket first — deepest senate books
and the only venue carrying groupItemTitle candidate metadata), falling back
to first-seen for unknown venues. Callers opt in by passing
``venue_by_market``; without it every market ranks equal and first-seen wins.

Some seats never get a party-direct market — Alaska 2026 trades only as
candidate-name markets ("Will Dan Sullivan win the Alaska Senate race in
2026?") on every ingested venue. Those titles carry no party, so mapping them
needs an external party signal, resolved in this order:

  1. a party suffix on the candidate label itself — Polymarket's
     ``groupItemTitle`` is "Jon Husted (R)" once nominees are named, and some
     titles embed it too (``candidate_party``);
  2. a caller-supplied hint (``extract_contested_seats``'s
     ``party_by_market``) — callers with richer context build it via
     ``resolve_candidate_party``, e.g. from the index's own audited
     ``republican_on_yes`` factor evaluation (1.0 ⇒ the YES side is the
     Republican ⇒ party "R").

Candidate markets whose party stays unresolved are skipped (graceful
degradation, never a guess), and a state's party-direct markets always beat
its candidate-name markets.

A third, lowest-precedence tier (2026-06-12) is the audited LLM extraction
fallback: markets no pattern understands can carry a ``seat_race_extraction``
factor evaluation whose value_label holds ``{state, party, race_type}``
(validated by :func:`parse_extraction_label` — state must be a real US state,
party exactly R/D). Extraction-derived seats only fill states that have no
pattern/candidate seat, so the LLM extends coverage but can never override a
deterministic parse. See the "extraction" factor type in dsl/ir.py. Within one (state, party) the highest-priced
candidate is the representative — P(R wins) is approximated by that single
candidate's price, which is exact whenever the party fields one serious
candidate (the empirical case; a multi-candidate sum would decouple prob_r
from the representative market's price and break delta_14d attribution).

Deliberately OUT of scope here (different aggregation, future work):
  * "an independent win…" markets — neither price nor 1 − price is P(R)
  * chamber-level seat-count brackets ("47 or fewer Senate seats") — those
    want a `negRisk` / `condition_id` partition aggregation, not a per-seat
    Bernoulli. Persisting `condition_id` for that path is tracked separately.

Sibling module ``state_lean`` (the MAGA choropleth) parses the same per-state
race titles but collapses DIFFERENTLY on purpose: it takes a volume-weighted
MEAN across every matching market in a state, whereas this module picks ONE
representative market per (state, party) by venue priority so ``prob_r`` stays
attributable to a single price (delta_14d needs that anchor). Expect the two to
report slightly different per-state numbers; unifying them onto
:func:`classify_markets` is tracked but deferred (it would shift published
numbers and needs sign-off).

Pure-Python, no DB / network — unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pmi_core.engine.candidate_parties import normalize_name, override_party

# Party-direct per-state race titles, one pattern per venue phrasing. All
# use named groups so party/state order can differ per venue (PredictIt puts
# the party in the trailing outcome leg).
_SEAT_RACE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # polymarket: "Will the Republicans win the North Carolina Senate race in 2026?"
    re.compile(
        r"will the (?P<party>republicans|democrats)"
        r" win the (?P<state>.+?) senate race in 2026",
        re.IGNORECASE,
    ),
    # forecastex: "Will the Republican party win the Texas general election
    # for US Senate in 2026?" — the "general election" tail keeps primary
    # markets ("... Republican primary election for US Senate ...") out.
    re.compile(
        r"will the (?P<party>republican|democratic) party"
        r" win the (?P<state>.+?) general election for us senate in 2026",
        re.IGNORECASE,
    ),
    # forecastex party-proxy: "Will Democratic Primary Winner win the Wyoming
    # general election for US Senate in 2026?" — whoever wins the primary IS
    # the nominee, so this is exactly P(party wins the seat).
    re.compile(
        r"will (?:the )?(?P<party>republican|democratic) primary winner"
        r" win the (?P<state>.+?) general election for us senate in 2026",
        re.IGNORECASE,
    ),
    # predictit: "Which party will win the 2026 US Senate election in
    # Maine? - Republican" — one core_markets row per outcome leg, so the
    # trailing leg label carries the party.
    re.compile(
        r"which party will win the 2026 us senate election in (?P<state>.+?)"
        r"\?\s*[-–]\s*(?P<party>republican|democratic)\s*$",
        re.IGNORECASE,
    ),
    # kalshi: "Will Democratics win the Senate race in Alabama? - Democratic
    # party" — the party-AGGREGATE outcome leg (P(party wins the seat)).
    # Yearless. The party comes from the trailing "<party> party" leg, NOT the
    # question subject (Kalshi misspells it "Democratics" and tags Minnesota as
    # "Democratic (DFL)"). Candidate legs ("- Hallie Shoffner") lack the
    # "party" tail and are deliberately skipped — only this aggregate leg is a
    # clean per-seat party probability.
    re.compile(
        r"will .+? win the senate race in (?P<state>[^?]+?)\?"
        r"\s*[-–]\s*(?P<party>republican|democratic)(?:\s*\([a-z]+\))?\s+party\s*$",
        re.IGNORECASE,
    ),
    # kalshi party-aggregate leg annotated with the likely NOMINEE instead of a
    # "- <party> party" tail ("Will Republicans win the Senate race in
    # Minnesota? - Michele Tafoya"): the party is in the SUBJECT. Read it there —
    # a state whose only kalshi legs carry candidate annotations (no clean
    # "- party" leg) would otherwise be dropped (e.g. SD-AL on the House side).
    # Candidate-SUBJECT legs ("Will John Fetterman win the Senate race in PA?")
    # don't match — the subject must be a party word — so they still fall through
    # to the candidate tier. Tried AFTER the "- party" leg so that stays
    # authoritative when both exist.
    re.compile(
        r"will (?:the |a )?(?P<party>republicans?|democratics?|democrats?)"
        r" win the senate race in (?P<state>[^?]+?)\?",
        re.IGNORECASE,
    ),
)

# Candidate-name general-election titles. Two tails: Polymarket's
# "<state> Senate race in 2026" and ForecastEx's "<state> general election
# for US Senate in 2026". Primaries / nominee markets / foreign senates stay
# rejected by the tails + state check; the subject guard below rejects
# non-name subjects ("the Republicans", "an independent", "a Democrat",
# ForecastEx's "Democratic Primary Winner" placeholder).
_CANDIDATE_RACE_RE = re.compile(
    r"will (.+?) win the (.+?)"
    r" (?:senate race in 2026|general election for us senate in 2026)",
    re.IGNORECASE,
)
_GENERIC_SUBJECT_RE = re.compile(
    r"^(?:(?:the|an|a)\s|(?:republican|democratic)\s+primary\s+winner\s*$)",
    re.IGNORECASE,
)

# Cross-venue representative pick: lower index wins a (state, party) side.
# Polymarket first — deepest senate orderbooks and the only venue whose raw
# payload carries groupItemTitle candidate names. Venues not listed (and
# callers that pass no venue map) rank last, where first-seen wins.
VENUE_PRIORITY: tuple[str, ...] = ("polymarket", "forecastex", "predictit", "kalshi")


def _venue_rank(venue: str | None) -> int:
    try:
        return VENUE_PRIORITY.index(venue)  # type: ignore[arg-type]
    except ValueError:
        return len(VENUE_PRIORITY)

# Trailing party tag on a candidate label: "Jon Husted (R)", "Roy Cooper (D)",
# tolerant of "(R-OH)" / "(REP)" / "(Republican)" variants. "(I)" and other
# parties deliberately don't match — only R/D resolve to a seat probability.
_PARTY_SUFFIX_RE = re.compile(
    r"\((R|D|REP|DEM|GOP|Republican|Democrat(?:ic)?)(?:[-–][A-Z]{2})?\)\s*$",
    re.IGNORECASE,
)

# US state / DC name → 2-letter code (mirror of the design's map.jsx
# stateNameToCode, used to populate the board's prob_by_state for the map).
_STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


@dataclass(frozen=True)
class ContestedSeat:
    """One contested Senate seat, collapsed from its per-party market(s)."""

    state: str            # canonical Title-Case state name, e.g. "North Carolina"
    state_code: str | None  # 2-letter code, e.g. "NC" (None if unrecognized)
    prob_r: float         # P(Republican wins the seat), in [0, 1]
    market_id: int        # representative market (the one whose price set prob_r)
    source_party: str     # "R" (used R market directly) or "D" (used 1 - D price)
    # Both underlying per-party markets (either may be None when only one side
    # trades). Lets the senate-board attribute matchup / contract count /
    # exchanges per seat without re-parsing titles (CORR-1.3 step 2).
    r_market_id: int | None = None
    d_market_id: int | None = None


# Public alias so sibling engines (e.g. state_lean) can scan titles for any
# state name without reaching into a private symbol.
STATE_NAME_TO_CODE = _STATE_NAME_TO_CODE


# 2020-census apportionment: voting US House seats per state, in effect for the
# 2022–2030 elections (so the 2026 cycle). Sum = 435. Keyed by 2-letter code; DC
# and territories are absent (non-voting delegations, no numbered district).
# The ceiling for a valid district number is PER STATE, not a single global max:
# a "Washington 34" scraped out of a "vote count above 340K" market clears a
# global 1..53 clamp but must be rejected because WA only has 10 districts.
HOUSE_SEATS: dict[str, int] = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1,
    "FL": 28, "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4,
    "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4,
    "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26,
    "NC": 14, "ND": 1, "OH": 15, "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7,
    "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2,
    "WI": 8, "WY": 1,
}


def state_code(name: str) -> str | None:
    """2-letter code for a state name (case-insensitive), or None."""
    return _STATE_NAME_TO_CODE.get(name.strip().lower())


def parse_seat_race(title: str) -> tuple[str, str] | None:
    """Return (party, state) for a per-state general-election race title.

    party is "R" or "D"; state is canonical Title-Case. Recognises one
    phrasing per venue (see ``_SEAT_RACE_PATTERNS``). Returns None for any
    title that isn't a party-direct per-state senate race (nominee markets,
    candidate-name markets, foreign senates, brackets, procedural, etc.).
    """
    for pattern in _SEAT_RACE_PATTERNS:
        m = pattern.search(title)
        if m is None:
            continue
        party = "R" if m.group("party").lower().startswith("r") else "D"
        state = m.group("state").strip().title()
        return party, state
    return None


def parse_candidate_race(title: str) -> tuple[str, str] | None:
    """Return (candidate, state) for a candidate-name general-election title.

    Matches "Will Dan Sullivan win the Alaska Senate race in 2026?" — the same
    "<state> Senate race in 2026" tail as :func:`parse_seat_race`, but with a
    candidate name as subject. Returns None for party-direct titles ("the
    Republicans"), independent markets ("an independent"), and anything whose
    state isn't a recognised US state (foreign senates won't sneak in via the
    looser subject).
    """
    m = _CANDIDATE_RACE_RE.search(title)
    if m is None:
        return None
    candidate = m.group(1).strip()
    if _GENERIC_SUBJECT_RE.match(candidate):
        return None
    state = m.group(2).strip().title()
    if state_code(state) is None:
        return None
    return candidate, state


def candidate_party(label: str | None) -> str | None:
    """"R" / "D" from a trailing party tag on a candidate label, else None.

    Reads Polymarket's ``groupItemTitle`` convention ("Jon Husted (R)") and
    title-embedded variants. Independents and unresolvable labels yield None.
    """
    if not label:
        return None
    m = _PARTY_SUFFIX_RE.search(label.strip())
    if m is None:
        return None
    return "R" if m.group(1)[0].upper() in ("R", "G") else "D"


def parse_extraction_label(label: str | None) -> tuple[str, str] | None:
    """(party, state) from a ``seat_race_extraction`` factor's value_label.

    The label is the canonical JSON the extraction factor type produces
    (llm/base.py): ``{"party": "R", "race_type": ..., "state": "Ohio"}``.
    This is where DOMAIN validation happens — the LLM layer stays generic:

      * party must be exactly "R" or "D"
      * state must resolve through the 50-state map (hallucinated or foreign
        "states" are dropped, never guessed)

    Returns None for anything that doesn't validate.
    """
    if not label:
        return None
    try:
        payload = json.loads(label)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    party = payload.get("party")
    state = str(payload.get("state") or "").strip().title()
    if party not in ("R", "D") or not state or state_code(state) is None:
        return None
    return party, state


def resolve_candidate_party(
    label: str | None, republican_on_yes: float | None = None
) -> str | None:
    """Best-effort party for a candidate market, for ``party_by_market`` hints.

    Prefers the explicit party tag on ``label`` (groupItemTitle); falls back
    to the index's audited ``republican_on_yes`` factor evaluation — 1.0 means
    the LLM judged the YES side to be the Republican, so party "R". The
    converse does NOT hold (0.0 covers Democrats, independents and write-ins
    alike), so a 0.0 never resolves to "D". Returns None when no signal."""
    party = candidate_party(label)
    if party is not None:
        return party
    if republican_on_yes is not None and republican_on_yes >= 0.5:
        return "R"
    return None


@dataclass
class _Side:
    """Best market so far for one (state, party) side."""

    price: float
    market_id: int
    rank: int  # venue priority rank; lower wins


@dataclass(frozen=True)
class EntityObservation:
    """One market classified into the entity space (CORR-1.8 extraction stage).

    The intermediate between "raw selected markets" and a collapsed
    representative: ``key`` is the group_by entity ("Ohio" for us_state,
    "California-20" for us_house_district), ``party`` is the side whose win
    the market's YES tracks, ``prob_r`` is the directed P(R wins the entity)
    read off this single market (price for an R-side market, 1 − price for a
    D-side one). ``seat_projection_sum`` collapses observations to one
    representative per entity (:func:`merge_observations`);
    ``entity_lean_mean`` averages them per entity instead.
    """

    market_id: int
    key: str
    code: str | None
    party: str          # "R" / "D"
    price: float        # raw market YES price in [0, 1]
    tier: str           # "patterns" | "candidates" | "llm_fallback"
    venue_rank: int
    # Candidate-name legs only: the candidate as the venue prints it ("Connie
    # Chan"), so the same person listed on several venues can be recognised
    # (see ``candidate_identity`` / the same-party fold in ``reduce_races``).
    label: str | None = None
    # Provenance when it differs from ``tier``: an LLM extraction that named a
    # CANDIDATE leg is consumed in the candidates tier (summed by party) but
    # was resolved via ``llm_fallback``. None ⇒ same as ``tier``.
    resolved_via: str | None = None

    @property
    def prob_r(self) -> float:
        directed = self.price if self.party == "R" else 1.0 - self.price
        return max(0.0, min(1.0, directed))


# ---------------------------------------------------------------------------
# Cross-venue race reduction (Stage C) — one P(R) per race, merging ALL venues
# ---------------------------------------------------------------------------

# Tiers whose observations are ALREADY a directed P(R) reading (an R/D party leg
# or the LLM's directed extraction). Candidate-name legs are handled separately
# (summed by party) because a single candidate's win price is NOT P(party wins).
_PARTY_DIRECT_TIERS = ("patterns", "llm_fallback")

# Within one venue the tiers are consumed in precedence order: a deterministic
# party leg beats the candidate sum, which beats an LLM-extracted directed
# reading. Before 2026-10-12 ``llm_fallback`` legs were averaged in WITH the
# patterns legs, so one venue's noise ("Will Talarico outperform the polling
# average?", a multi-race combo, a vote-share rung) moved a race the venue had
# already priced cleanly: Texas read 60 R while every venue's winner leg sat at
# 37–38 (CORR-1.11).
_VENUE_TIER_ORDER = ("patterns", "candidates", "llm_fallback")

# A race whose only legs are candidates of ONE party, whose pooled win-mass
# reaches this floor, has no other-party path — a top-two same-party general
# (CA-11: Wiener vs Chan, both D). Per-venue "1 − D mass" is wrong there when a
# venue lists only one of the two candidates (polymarket Chan 0.78 → P(R) 0.22).
_SAME_PARTY_MASS_FLOOR = 0.9

# Candidate name out of a title no grammar understood: the leg after the last
# dash ("California-11 House Winner - Scott Wiener", "CA-11 General Election —
# Connie Chan", "Who will win the CA-11 House election? - Connie Chan") or the
# "Will <name> win/receive …" subject.
_TAIL_LABEL_RE = re.compile(r"[-–—]\s*([^-–—?]+?)\s*$")
_SUBJECT_LABEL_RE = re.compile(r"^\s*will (.+?) (?:win|receive|be |get )", re.IGNORECASE)


def candidate_label_from_title(title: str | None) -> str | None:
    """Best-effort candidate label for a leg whose title carries one, else None."""
    if not title:
        return None
    m = _TAIL_LABEL_RE.search(title)
    if m:
        tail = m.group(1).strip()
        # "- Republican party" / "- Democratic" tails are party legs, not names.
        if tail and not re.search(r"\b(?:party|republican|democrat)", tail, re.IGNORECASE):
            return tail
    m = _SUBJECT_LABEL_RE.match(title)
    if m:
        subj = m.group(1).strip()
        if subj and not _GENERIC_SUBJECT_RE.match(subj) and subj.lower() not in (
            "republicans", "democrats", "democratics", "republican", "democratic", "gop"
        ):
            return subj
    return None


def candidate_identity(obs: EntityObservation) -> str:
    """Cross-venue identity of a candidate leg: the surname of its label, so
    "Connie Chan" (polymarket) and "Connie Chan" (forecastex) pool as one
    candidate. A leg with no label is its own identity (never merged)."""
    name = normalize_name(obs.label)
    if not name:
        return f"#{obs.market_id}"
    return name.split()[-1]


@dataclass(frozen=True)
class RaceReduction:
    """One P(R wins the seat) per entity, merged across every venue that trades it.

    Replaces the two lossy paths used before:
      * ``merge_observations`` representative-pick (one venue, one market), and
      * ``entity_lean``'s flat mean of every observation's ``prob_r`` — which
        double-counted a race's multiple same-party candidates and folded a lone
        D candidate in as ``1 − price``, giving e.g. TX-2 = 0.38 when the real R
        lean was ~0.68.

    Two levels:
      1. within a venue → one P(R):
         * party-direct legs (``patterns`` / ``llm_fallback``): mean of their
           directed ``prob_r`` — one R + one D leg ⇒ ``avg(r, 1 − d)``;
         * candidate legs (``candidates``): the candidates in one race are
           mutually exclusive, so sum their raw win prices by party and
           normalise, ``P(R) = ΣR / (ΣR + ΣD)``;
         * a venue with both prefers its party-direct legs (the cleaner signal).
      2. across venues → liquidity-weighted mean of the per-venue P(R) (deep
         books dominate; simple mean when no venue carries liquidity, e.g. an
         all-play-money race).
    """

    key: str
    code: str | None
    p_r: float
    weight: float           # summed liquidity behind this P(R) (0 = none / play-money)
    market_ids: list[int]   # every market that fed it — lineage + drill-down
    venue_counts: dict[str, int]
    # D10 lineage: each venue's P(R) and its normalized share of the cross-venue
    # fold (shares sum to 1 and reconcile exactly: Σ share·p_r = the race p_r).
    # Venue-level on purpose — candidate legs SUM by party inside a venue, so a
    # per-market weighted-mean decomposition below this level does not exist.
    venue_lineage: list = field(default_factory=list)


def _venue_pr(
    obs: list[EntityObservation], liquidity_by_market: dict[int, float]
) -> tuple[float, float, list[int]] | None:
    """(p_r, weight, market_ids) for one venue's observations of one race, or None.

    Prefers party-direct legs; falls back to the candidate sum-by-party. None
    when neither yields a usable reading (no direct leg and zero candidate mass).
    """
    by_tier = {t: [o for o in obs if o.tier == t] for t in _VENUE_TIER_ORDER}
    used = next((legs for t in _VENUE_TIER_ORDER if (legs := by_tier[t])), [])
    if not used:
        return None
    direct = used[0].tier in _PARTY_DIRECT_TIERS
    if direct:
        p_r = sum(o.prob_r for o in used) / len(used)
    else:
        r_mass = sum(o.price for o in used if o.party == "R")
        d_mass = sum(o.price for o in used if o.party == "D")
        if r_mass + d_mass <= 0:
            return None
        if r_mass > 0 and d_mass > 0:
            # Both sides listed → the candidates span the field; normalise so the
            # two mutually-exclusive parties' win-mass sums to 1 (TX-2: two R +
            # one D → 0.26/(0.26+0.12) = 0.68).
            p_r = r_mass / (r_mass + d_mass)
        elif r_mass > 0:
            # Only R candidates listed (the opponent isn't on this venue) →
            # normalising would force P(R)=1. The summed R win-prob IS the
            # estimate; a lone Sullivan @0.375 stays 0.375, not 1.0.
            p_r = min(r_mass, 1.0)
        else:
            # Only D candidates → P(R) is the complement of the D win-mass.
            p_r = 1.0 - min(d_mass, 1.0)
    weight = sum(liquidity_by_market.get(o.market_id, 0.0) for o in used)
    return max(0.0, min(1.0, p_r)), weight, [o.market_id for o in used]


def _same_party_pr(obs: list[EntityObservation]) -> float | None:
    """P(R) for a race whose candidate legs all belong to one party and span
    the field, else None (keep the per-venue fold).

    Pools each candidate's price ACROSS venues (mean per identity — the same
    person on three venues is one candidate, not three) and sums the pool per
    party. Only fires when no leg of the other party exists on any venue and no
    directed (party) leg exists at all. A lone favourite at 0.95 yields the
    same 0.05 either way; a lone candidate at 0.45 (mass < 0.9) keeps the
    per-venue complement — the other party's candidate just isn't listed.
    """
    if any(o.tier in _PARTY_DIRECT_TIERS for o in obs):
        return None
    cands = [o for o in obs if o.tier == "candidates"]
    if not cands:
        return None
    parties = {o.party for o in cands}
    if len(parties) != 1:
        return None
    by_id: dict[str, list[float]] = {}
    for o in cands:
        by_id.setdefault(candidate_identity(o), []).append(o.price)
    mass = sum(sum(ps) / len(ps) for ps in by_id.values())
    if mass < _SAME_PARTY_MASS_FLOOR:
        return None
    party = parties.pop()
    return min(1.0, mass) if party == "R" else max(0.0, 1.0 - mass)


def reduce_races(
    observations: list[EntityObservation],
    venue_by_market: dict[int, str | None],
    liquidity_by_market: dict[int, float],
) -> dict[str, RaceReduction]:
    """Collapse classified observations into one ``RaceReduction`` per entity key.

    The shared per-race reduction both ``entity_lean_mean`` (averages the P(R)s)
    and ``seat_projection_sum`` (feeds them to the Poisson-binomial) consume, so
    the map and the seat board agree on how a race's cross-venue price is formed.
    """
    by_key: dict[str, list[EntityObservation]] = {}
    for o in observations:
        by_key.setdefault(o.key, []).append(o)

    out: dict[str, RaceReduction] = {}
    for key, obs in by_key.items():
        by_venue: dict[str, list[EntityObservation]] = {}
        for o in obs:
            by_venue.setdefault(venue_by_market.get(o.market_id) or "", []).append(o)

        per_venue: list[tuple[str, float, float, list[int]]] = []
        for venue, vobs in by_venue.items():
            reduced = _venue_pr(vobs, liquidity_by_market)
            if reduced is not None:
                per_venue.append((venue, *reduced))
        if not per_venue:
            continue

        total_w = sum(w for _, _, w, _ in per_venue)
        if total_w > 0:
            p_r = sum(pr * w for _, pr, w, _ in per_venue) / total_w
        else:
            p_r = sum(pr for _, pr, _, _ in per_venue) / len(per_venue)

        # Same-party race (top-two general): no directed leg anywhere, every
        # candidate of ONE party, and their pooled win-mass spans the field →
        # the other party has no candidate, so P(R) is 0 (or 1), not the
        # per-venue complement of whichever candidate that venue happens to list.
        same_party = _same_party_pr(obs)
        if same_party is not None:
            p_r = same_party

        shares = (
            [(v, pr, w / total_w) for v, pr, w, _ in per_venue]
            if total_w > 0
            else [(v, pr, 1.0 / len(per_venue)) for v, pr, _, _ in per_venue]
        )
        venue_lineage = [
            {"venue": v or None, "p_r": round(pr, 4), "share": round(s, 4)}
            for v, pr, s in sorted(shares, key=lambda x: -x[2])
        ]

        market_ids = [mid for _, _, _, mids in per_venue for mid in mids]
        venue_counts: dict[str, int] = {}
        for venue, _, _, mids in per_venue:
            if venue:
                venue_counts[venue] = venue_counts.get(venue, 0) + len(mids)

        out[key] = RaceReduction(
            key=key,
            code=obs[0].code,
            p_r=max(0.0, min(1.0, p_r)),
            weight=total_w,
            market_ids=market_ids,
            venue_counts=venue_counts,
            venue_lineage=venue_lineage,
        )
    return out


def classify_markets(
    markets: list[tuple[int, str, float]],
    *,
    parse_direct=None,
    parse_candidate=None,
    key_code=state_code,
    party_by_market: dict[int, str] | None = None,
    venue_by_market: dict[int, str | None] | None = None,
    extraction_by_market: dict[int, tuple[str, str]] | None = None,
    extraction_kinds: dict[int, str] | None = None,
    office: str | None = None,
) -> list[EntityObservation]:
    """Classify (market_id, title, price) triples into entity observations.

    Grammar-agnostic core of the extraction stage: the entity key space comes
    entirely from the supplied callbacks, so the senate (us_state) and House
    district grammars share this one implementation.

      * ``parse_direct(title) → (party, key) | None`` — party-direct titles;
        pass None to disable the patterns tier.
      * ``parse_candidate(title) → (label, key) | None`` — candidate-name
        titles; party resolves from the label's tag (:func:`candidate_party`)
        falling back to ``party_by_market``; unresolvable candidates are
        skipped, never guessed. Pass None to disable the candidates tier.
      * ``extraction_by_market`` maps market_id → (party, key) from the
        index's audited ``type: extraction`` factor — the llm_fallback tier;
        omit to disable. Consulted only for markets neither parser claimed.
      * ``extraction_kinds`` maps market_id → the extraction's ``race_type``
        ("party_direct" | "candidate"). An LLM-extracted CANDIDATE leg ("Who
        will win the CA-11 House election? - Connie Chan") is consumed in the
        candidates tier — its price is one candidate's win chance, summed by
        party with its rivals — not as a directed P(party wins) reading. Before
        2026-10-12 every extraction was treated as directed, so a D-vs-D race
        read as ``1 − price`` per candidate (CA-11: 25 R with no Republican on
        the ballot). Provenance stays ``resolved_via="llm_fallback"``.
      * ``office`` scopes the curated ``candidate_parties`` override lookup
        (senate / house / governor); inferred from the key when omitted.
      * ``key_code(key) → str | None`` derives the short display code.

    A market is claimed by the FIRST tier that understands it (patterns >
    candidates > llm_fallback), so the LLM can never relabel a market a
    deterministic parse already placed. A candidate's party resolves in order:
    the curated override table → the label's party tag → the caller's hint →
    the extraction's party. An override of "I" (independent) drops the leg.
    """
    hints = party_by_market or {}
    venues = venue_by_market or {}
    extractions = extraction_by_market or {}
    kinds = extraction_kinds or {}
    out: list[EntityObservation] = []

    for market_id, title, price in markets:
        rank = _venue_rank(venues.get(market_id))
        parsed = parse_direct(title) if parse_direct is not None else None
        # A grammar may return a single (party, key) tuple (senate/house/
        # governor — one entity per title) or a list of them (the war country
        # grammar — a bilateral title attributes to every named country). An
        # empty list means "no patterns claim", so fall through to the lower
        # tiers exactly like None.
        pairs = (
            parsed
            if isinstance(parsed, list)
            else ([parsed] if parsed is not None else [])
        )
        if pairs:
            for party, key in pairs:
                out.append(
                    EntityObservation(
                        market_id, key, key_code(key), party, price, "patterns", rank
                    )
                )
            continue
        parsed_cand = parse_candidate(title) if parse_candidate is not None else None
        if parsed_cand is not None:
            name, key = parsed_cand
            override = override_party(name, key, office)
            if override == "I":
                continue  # independent / third-party: no party to credit
            party = override or candidate_party(name) or hints.get(market_id)
            if party not in ("R", "D"):
                # republican_on_yes=0.0 can't tell a Democrat from an independent,
                # so party_by_market leaves these unresolved — but the audited
                # extraction factor named the party outright (the D-vs-D case:
                # neither YES side is Republican, yet each has a definite party).
                # Use this market's own extraction party before skipping, keeping
                # it in the candidates tier (its price semantics are unchanged).
                ext = extractions.get(market_id)
                if ext is not None:
                    party = ext[0]
            if party not in ("R", "D"):
                continue
            out.append(
                EntityObservation(
                    market_id, key, key_code(key), party, price, "candidates", rank,
                    label=name,
                )
            )
            continue
        ext = extractions.get(market_id)
        if ext is None:
            continue
        party, key = ext
        if kinds.get(market_id) == "candidate":
            # The LLM named a candidate leg: one person's win chance. Fold it
            # with the candidates tier (summed by party), never as directed.
            label = candidate_label_from_title(title)
            override = override_party(label, key, office)
            if override == "I":
                continue
            if override in ("R", "D"):
                party = override
            out.append(
                EntityObservation(
                    market_id, key, key_code(key), party, price, "candidates", rank,
                    label=label, resolved_via="llm_fallback",
                )
            )
            continue
        out.append(
            EntityObservation(market_id, key, key_code(key), party, price, "llm_fallback", rank)
        )
    return out


def merge_observations(observations: list[EntityObservation]) -> list[ContestedSeat]:
    """Collapse classified observations into one representative per entity.

    Precedence is per-entity by tier (patterns > candidates > llm_fallback):
    a lower tier only fills entities the upper tiers produced nothing for —
    it never merges sides into an entity an upper tier already covered.
    Within a (entity, party) side the highest-priority venue wins
    (``VENUE_PRIORITY``); within the same venue rank, the patterns tier keeps
    the first-seen market while the candidate/llm tiers keep the
    highest-priced one (the serious-candidate proxy). The two sides of an
    entity then collapse to a single directed probability, preferring the R
    market's price as the direct P(R wins) and using 1 − price otherwise.

    Returns entities sorted by key for deterministic output. (The
    ``ContestedSeat.state`` field carries the entity key — a state name for
    us_state, a "California-20" district key for us_house_district.)
    """
    tiers: dict[str, dict[str, dict[str, _Side]]] = {
        "patterns": {},
        "candidates": {},
        "llm_fallback": {},
    }
    code_by_key: dict[str, str | None] = {}
    for obs in observations:
        code_by_key.setdefault(obs.key, obs.code)
        sides = tiers[obs.tier].setdefault(obs.key, {})
        best = sides.get(obs.party)
        if obs.tier == "patterns":
            replace = best is None or obs.venue_rank < best.rank
        else:
            replace = (
                best is None
                or obs.venue_rank < best.rank
                or (obs.venue_rank == best.rank and obs.price > best.price)
            )
        if replace:
            sides[obs.party] = _Side(obs.price, obs.market_id, obs.venue_rank)

    by_key: dict[str, dict[str, _Side]] = tiers["patterns"]
    for tier_name in ("candidates", "llm_fallback"):
        for key, sides in tiers[tier_name].items():
            if key not in by_key:
                by_key[key] = sides

    seats: list[ContestedSeat] = []
    for key in sorted(by_key):
        sides = by_key[key]
        r, d = sides.get("R"), sides.get("D")
        if r is not None:
            p_r = max(0.0, min(1.0, r.price))
            seats.append(
                ContestedSeat(
                    key, code_by_key.get(key), p_r, r.market_id, "R",
                    r_market_id=r.market_id,
                    d_market_id=d.market_id if d else None,
                )
            )
        elif d is not None:
            p_r = max(0.0, min(1.0, 1.0 - d.price))
            seats.append(
                ContestedSeat(
                    key, code_by_key.get(key), p_r, d.market_id, "D",
                    r_market_id=None, d_market_id=d.market_id,
                )
            )
    return seats


def extract_contested_seats(
    markets: list[tuple[int, str, float]],
    party_by_market: dict[int, str] | None = None,
    venue_by_market: dict[int, str | None] | None = None,
    extraction_by_market: dict[int, tuple[str, str]] | None = None,
) -> list[ContestedSeat]:
    """Collapse per-party race markets into one ContestedSeat per state.

    ``markets`` is a list of (market_id, title, price) where price is the
    market's latest YES probability in [0, 1]. Non-matching titles are
    ignored. States with both an R and a D market collapse to one seat,
    preferring the R market's price as the direct P(R wins).

    ``party_by_market`` maps market_id → "R"/"D" for candidate-name markets
    whose party isn't in the title (build it with
    :func:`resolve_candidate_party`). Candidate markets only fill states with
    NO party-direct market; per (state, party) the highest-priced candidate
    is the representative. Candidate markets with no resolvable party are
    skipped.

    ``venue_by_market`` maps market_id → venue for the cross-venue merge:
    when one (state, party) side trades on several venues, the market on the
    highest-priority venue (``VENUE_PRIORITY``) is the representative —
    never an average, so prob_r stays attributable to one market's snapshot
    history. Without the map every market ranks equal and first-seen wins.

    ``extraction_by_market`` maps market_id → (party, state) from the audited
    ``seat_race_extraction`` LLM factor (build it with
    :func:`parse_extraction_label`). It is the LOWEST precedence tier: a
    market that any title pattern or the candidate path already understood
    keeps that deterministic reading, and an extraction-derived seat only
    fills states with no pattern/candidate seat at all. This keeps the LLM
    strictly additive — it can extend coverage to phrasings the patterns
    don't know yet, but can never override or corrupt an exactly-parsed seat.

    Returns seats sorted by state name for deterministic output.

    (This is the ``group_by: us_state`` grammar bound to the generic
    :func:`classify_markets` + :func:`merge_observations` extraction core —
    CORR-1.8 made the stage declarative; see ``engine/extraction.py``.)
    """
    return merge_observations(
        classify_markets(
            markets,
            parse_direct=parse_seat_race,
            parse_candidate=parse_candidate_race,
            key_code=state_code,
            party_by_market=party_by_market,
            venue_by_market=venue_by_market,
            extraction_by_market=extraction_by_market,
        )
    )
