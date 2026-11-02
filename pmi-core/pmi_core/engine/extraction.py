"""Declarative entity-extraction stage (CORR-1.8, 2026-06-12 design 拍板).

Unifies the structural aggregation paths into one pipeline shape:

    selector → (extraction?) → formula

An index declares ``extraction: {group_by, sides, methods}`` in YAML
(dsl/ir.py ``ExtractionSpec``); this module turns the selected markets into
per-entity directed observations and representative collapses, and the
formula (``seat_projection_sum`` / ``entity_lean_mean``) consumes entities —
no engine code per index. The north star: the Nth same-shaped index is pure
YAML.

``group_by`` names both the entity key space and the title grammar:

  * ``us_state`` — per-state 2026 *Senate* races, exactly the
    ``seat_mapping`` grammar (one Class-II seat per state, so the state IS
    the seat). Patterns / candidate / LLM tiers unchanged.
  * ``us_house_district`` — per-district 2026 US House races. Live coverage
    is ForecastEx (the only ingested venue with per-district win markets):

        party-direct: "Will the Republican party win the California's 20th
                       District general election for the US House of
                       Representatives in 2026?"
        party-proxy:  "Will Democratic Primary Winner win the Alabama 5th
                       District general election for the US House of
                       Representatives in 2026?"  (the primary winner IS the
                       nominee, so this is P(party wins the district))
        candidate:    "Will Adam Gray win the California 13th District
                       general election for the US House of Representatives
                       in 2026?"

    Kalshi adds an abbreviated code form with a party-aggregate outcome leg:
        "Will Democratic win the House race for AL-01? - Democratic party"
    (party reads off the trailing "<party> party" leg; candidate legs are
    skipped). Polymarket's "TX-25 House seat" code form is also parsed.
    Note the possessive: party-direct titles write "California's 20th",
    proxy/candidate ones "California 13th" — the patterns accept both.
    Primaries stay out via the literal "general election" tail.

The entity key for a district is ``"<State>-<district>"`` ("California-20",
at-large "North Dakota-AL"), code ``"CA-20"`` / ``"ND-AL"``.

The classify/merge core lives in ``seat_mapping`` (battle-tested by the
senate board); this module binds grammars to it and dispatches on the spec.

Pure-Python, no DB / network — unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from pmi_core.dsl.ir import ExtractionSpec
from pmi_core.engine.geo import (
    match_countries,
    region_of,
    resolve_country,
)
from pmi_core.engine.race_calendar import (
    governor_states_for_cycle,
    senate_states_for_cycle,
)
from pmi_core.engine.seat_mapping import (
    _GENERIC_SUBJECT_RE,
    HOUSE_SEATS,
    STATE_NAME_TO_CODE,
    ContestedSeat,
    EntityObservation,
    classify_markets,
    merge_observations,
    parse_candidate_race,
    parse_extraction_label,
    parse_seat_race,
    state_code,
)

# 2-letter code → canonical Title-Case state name (reverse of STATE_NAME_TO_CODE),
# for abbreviated district codes like "TX-25".
_CODE_TO_STATE: dict[str, str] = {c: n.title() for n, c in STATE_NAME_TO_CODE.items()}

# ---------------------------------------------------------------------------
# us_house_district grammar
# ---------------------------------------------------------------------------

# "<State>('s) <Nth>|at-large District ... general election for the US House
# of Representatives in 2026". The lazy state group stops before an optional
# possessive ('s / ’s); ordinal suffix is parsed but not trusted (the digits
# rule).
_DISTRICT_TAIL = (
    r" district general election for the us house of representatives in 2026"
)
_DISTRICT_TOKEN = r"(?P<district>\d{1,2}(?:st|nd|rd|th)?|at[- ]large)"
_STATE_TOKEN = r"(?P<state>.+?)(?:['’]s)?"

_DISTRICT_RACE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # forecastex party-direct: "Will the Republican party win the
    # California's 20th District general election for the US House of
    # Representatives in 2026?"
    re.compile(
        rf"will the (?P<party>republican|democratic) party"
        rf" win the {_STATE_TOKEN} {_DISTRICT_TOKEN}{_DISTRICT_TAIL}",
        re.IGNORECASE,
    ),
    # forecastex party-proxy: "Will Democratic Primary Winner win the
    # Alabama 5th District general election ..." — the primary winner IS the
    # nominee, so this is exactly P(party wins the district).
    re.compile(
        rf"will (?:the )?(?P<party>republican|democratic) primary winner"
        rf" win the {_STATE_TOKEN} {_DISTRICT_TOKEN}{_DISTRICT_TAIL}",
        re.IGNORECASE,
    ),
)

# polymarket abbreviated party-direct: "Will the Republican Party win the TX-25
# House seat?" — the well-priced per-district P(party wins) source the legacy
# state_lean parsed but the verbose ForecastEx-only patterns above miss
# (issue.md §2 breadth gap). State is a 2-letter code, district is the number;
# yearless (polymarket omits the cycle) — accepted, same as state_lean did.
_DISTRICT_CODE_RE = re.compile(
    r"will the (?P<party>republican|democratic) party"
    r" win the (?P<code>[a-z]{2})-(?P<district>\d{1,2}|al) house seat",
    re.IGNORECASE,
)

# kalshi abbreviated per-district: "Will Democratic win the House race for
# AL-01? - Democratic party" — code form like polymarket's, but the party reads
# off the trailing "<party> party" AGGREGATE leg (P(party wins the district)),
# not the question subject. Candidate legs ("- Lee McInnis") lack the "party"
# tail and are skipped — only the aggregate leg is a clean per-district reading.
_DISTRICT_KALSHI_RE = re.compile(
    r"will .+? win the house race for (?P<code>[a-z]{2})-(?P<district>\d{1,2}|al)\?"
    r"\s*[-–]\s*(?P<party>republican|democratic)(?:\s*\([a-z]+\))?\s+party\s*$",
    re.IGNORECASE,
)

# kalshi party-aggregate leg annotated with the likely NOMINEE instead of a
# "- <party> party" tail ("Will Republican win the House race for SD-AL? - Marty
# Jackley"): the party is in the SUBJECT. Districts whose only kalshi legs carry
# candidate annotations (no clean "- party" leg) would otherwise be dropped —
# SD-AL was the reported miss. Candidate-SUBJECT legs ("Will Bill Hill win the
# House race for AK-AL?") don't match — the subject must be a party word — so
# they still fall through. Tried AFTER _DISTRICT_KALSHI_RE so the "- party" leg
# stays authoritative when both exist.
_DISTRICT_KALSHI_SUBJECT_RE = re.compile(
    r"will (?:the |a )?(?P<party>republicans?|democratics?|democrats?)"
    r" win the house race for (?P<code>[a-z]{2})-(?P<district>\d{1,2}|al)\?",
    re.IGNORECASE,
)

# Candidate-name district titles share the same tail; the generic-subject
# guard (reused from the senate grammar) rejects "the Republican party" /
# "Democratic Primary Winner" subjects so party-direct titles never land
# here, and the state check rejects anything not a real US state.
_DISTRICT_CANDIDATE_RE = re.compile(
    rf"will (?P<name>.+?) win the {_STATE_TOKEN} {_DISTRICT_TOKEN}{_DISTRICT_TAIL}",
    re.IGNORECASE,
)


def _district_key(state: str, district_token: str) -> tuple[str, str] | None:
    """(key, code) for a recognised (state, district token), else None.

    key = "California-20" / "North Dakota-1"; code = "CA-20" / "ND-1".
    The district number must be a valid seat for THAT state (1..HOUSE_SEATS[code],
    2020-census apportionment) — a per-state ceiling, not a global one. A single
    global 1..53 clamp let phantom districts through: a "Washington 34" scraped
    out of a "vote count above 340K" market passed 34 <= 53 and created a bogus
    WA-34 race (WA has only 10 districts), overshooting the 435-seat field.

    At-large states (one seat: AK/DE/ND/SD/VT/WY) canonicalise to district **1**,
    matching old micah's convention — so a ForecastEx "...1st District..." market
    and a polymarket "...XX-AL House seat..." market merge into ONE entity instead
    of splitting into XX-1 vs XX-AL.
    """
    state = state.strip().title()
    code = state_code(state)
    if code is None:
        return None
    seats = HOUSE_SEATS.get(code)
    if seats is None:
        return None  # DC / territories: no voting House district
    token = district_token.strip().lower().replace(" ", "-")
    if token in ("at-large", "al"):
        num = "1"
    else:
        digits = re.sub(r"(st|nd|rd|th)$", "", token)
        if not digits.isdigit() or not 1 <= int(digits) <= seats:
            return None
        num = str(int(digits))
    return f"{state}-{num}", f"{code}-{num}"


def district_code(key: str) -> str | None:
    """"CA-20" for an entity key "California-20" (None if unrecognized)."""
    state, _, num = key.rpartition("-")
    code = state_code(state)
    if code is None or not num:
        return None
    return f"{code}-{num}"


def parse_district_race(title: str) -> tuple[str, str] | None:
    """Return (party, district_key) for a party-direct/proxy district title.

    party is "R" or "D"; district_key is canonical "<State>-<n>" (at-large →
    "<State>-AL"). None for primaries (the "general election" tail), margin
    brackets, candidate-name titles, and unrecognised states/districts.
    """
    for pattern in _DISTRICT_RACE_PATTERNS:
        m = pattern.search(title)
        if m is None:
            continue
        keyed = _district_key(m.group("state"), m.group("district"))
        if keyed is None:
            return None
        party = "R" if m.group("party").lower().startswith("r") else "D"
        return party, keyed[0]
    # polymarket abbreviated "TX-25 House seat" + kalshi "House race for TX-25?
    # - <party> party" (party in the tail) + kalshi party-subject form (party in
    # the subject, candidate-annotated tail). State given as a 2-letter code.
    for code_re in (_DISTRICT_CODE_RE, _DISTRICT_KALSHI_RE, _DISTRICT_KALSHI_SUBJECT_RE):
        m = code_re.search(title)
        if m is None:
            continue
        state = _CODE_TO_STATE.get(m.group("code").upper())
        if state is None:
            return None
        keyed = _district_key(state, m.group("district"))
        if keyed is None:
            return None
        party = "R" if m.group("party").lower().startswith("r") else "D"
        return party, keyed[0]
    return None


def parse_district_candidate_race(title: str) -> tuple[str, str] | None:
    """Return (candidate, district_key) for a candidate-name district title."""
    m = _DISTRICT_CANDIDATE_RE.search(title)
    if m is None:
        return None
    candidate = m.group("name").strip()
    if _GENERIC_SUBJECT_RE.match(candidate):
        return None
    keyed = _district_key(m.group("state"), m.group("district"))
    if keyed is None:
        return None
    return candidate, keyed[0]


def extraction_race_type(label: str | None) -> str | None:
    """The extraction factor's ``race_type`` ("party_direct" | "candidate"),
    or None when absent / unrecognised. Grammar-agnostic: the seat, district
    and governor labels all carry the field (prompts/factors/*-extraction)."""
    if not label:
        return None
    try:
        payload = json.loads(label)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("race_type") or "").strip().lower()
    return kind if kind in ("party_direct", "candidate") else None


def parse_district_extraction_label(label: str | None) -> tuple[str, str] | None:
    """(party, district_key) from a district extraction factor's value_label.

    The label is the canonical JSON an ``extraction`` factor produces
    (llm/base.py): ``{"district": 20, "party": "R", "race_type": ...,
    "state": "California"}``. Domain validation mirrors
    :func:`seat_mapping.parse_extraction_label`: party exactly R/D, state a
    real US state, district 1..53 or "at-large"/"AL". Anything else → None,
    never a guess.
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
    if party not in ("R", "D"):
        return None
    keyed = _district_key(
        str(payload.get("state") or ""), str(payload.get("district") or "")
    )
    if keyed is None:
        return None
    return party, keyed[0]


# ---------------------------------------------------------------------------
# us_governor grammar
# ---------------------------------------------------------------------------

# One governorship per state, so (like us_state senate) the state IS the entity
# — same key space as us_state, distinct title grammar. Phrasings carried over
# from the live MAGA parser (state_lean): "<State> gubernatorial/governor race",
# "election for governor of <State>", plus the forecastex general-election form.
_GOV_OFFICE = r"(?:gubernatorial\s+race|governor(?:'s)?\s+race|governorship)"
_PARTY_WORDS = frozenset(
    {"republican", "republicans", "democrat", "democrats", "democratic", "gop"}
)

_GOVERNOR_RACE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # polymarket: "Will the Republicans win the Texas gubernatorial race in
    # 2026?" / "Will Republicans flip the Nevada governorship in 2026?"
    re.compile(
        rf"will (?:the )?(?P<party>republicans?|democrats?|gop)"
        rf" (?:win|hold|flip|keep|retain|gain)s? the (?P<state>.+?)"
        rf" {_GOV_OFFICE} in 2026",
        re.IGNORECASE,
    ),
    # forecastex: "Will the Republican party win the Texas general election for
    # governor in 2026?" (mirrors the senate "general election for US Senate" form)
    re.compile(
        r"will the (?P<party>republican|democratic) party"
        r" win the (?P<state>.+?) general election for governor in 2026",
        re.IGNORECASE,
    ),
    # predictit: "Which party will win the 2026 election for governor of Ohio?
    # - Democratic" — party in the trailing outcome leg.
    re.compile(
        r"which party will win the 2026 election for governor of (?P<state>.+?)"
        r"\?\s*[-–]\s*(?P<party>republican|democratic)\s*$",
        re.IGNORECASE,
    ),
    # kalshi: "Will the Democratic party win the governorship in Arizona" —
    # party is in the QUESTION; the price IS P(party wins) for that one (state,
    # party) row. A nominee may be appended as annotation ("... in Alabama -
    # Doug Jones") — harmless, the row is still the party probability, so the
    # trailing "- <name>" leg is consumed and ignored. The singular
    # "governorship in" keeps the multi-state combo ("governorships of PA, MI,
    # …") and foreign (Brazil) markets out.
    re.compile(
        r"will the (?P<party>republican|democratic) party"
        r" win the governorship in (?P<state>[a-z .]+?)(?:\s*[-–]\s*.+)?$",
        re.IGNORECASE,
    ),
)

# Candidate-name governor titles share the gubernatorial/governorship tail; the
# generic-subject guard rejects "the Republicans" subjects, and an extra bare
# party-word guard covers verbs outside the party-direct set.
_GOVERNOR_CANDIDATE_RE = re.compile(
    rf"will (?P<name>.+?) win the (?P<state>.+?) {_GOV_OFFICE} in 2026",
    re.IGNORECASE,
)


def _party_initial(word: str) -> str:
    """"R" for republican(s)/gop, "D" for democrat(s)/democratic."""
    return "R" if word[0].lower() in ("r", "g") else "D"


def parse_governor_race(title: str) -> tuple[str, str] | None:
    """Return (party, state) for a party-direct 2026 governor race title.

    party is "R"/"D"; state is canonical Title-Case. None for primaries,
    candidate-name titles, unrecognised states, and non-governor offices.
    """
    for pattern in _GOVERNOR_RACE_PATTERNS:
        m = pattern.search(title)
        if m is None:
            continue
        state = m.group("state").strip().title()
        if state_code(state) is None:
            return None
        return _party_initial(m.group("party")), state
    return None


def parse_governor_candidate_race(title: str) -> tuple[str, str] | None:
    """Return (candidate, state) for a candidate-name 2026 governor title."""
    m = _GOVERNOR_CANDIDATE_RE.search(title)
    if m is None:
        return None
    candidate = m.group("name").strip()
    if _GENERIC_SUBJECT_RE.match(candidate) or candidate.lower() in _PARTY_WORDS:
        return None
    state = m.group("state").strip().title()
    if state_code(state) is None:
        return None
    return candidate, state


# ---------------------------------------------------------------------------
# country grammar (war vertical, W2)
# ---------------------------------------------------------------------------

# The country grammar is partyless: an observation's key is a country slug and
# the escalation/peace direction is the index's `direction` factor (consumed by
# `geo_weighted_mean`), not a side collapsed here. So the parse callbacks return
# a sentinel party `_NO_SIDE`; classify_markets stores it verbatim (the patterns
# / llm_fallback tiers don't validate party — only the candidates tier needs
# R/D, and the country spec leaves candidates out of `methods`).
_NO_SIDE = "none"


def parse_country_direct(title: str) -> list[tuple[str, str]]:
    """One (sentinel, country_slug) per country a title names — possibly many.

    Patterns tier: the human-authored alias matcher (geo.match_countries) is
    many-to-many, so a bilateral title ("Russia invade Ukraine") is attributed
    to BOTH Russia and Ukraine — the legacy war index does the same, and it's
    the bilateral markets that carry the index. classify_markets emits one
    observation per pair. Returns ``[]`` (no claim → falls through to the
    llm_fallback tier) for titles that name no country or are meeting venues.
    """
    return [(_NO_SIDE, slug) for slug in match_countries(title)]


def _no_candidate(_title: str) -> tuple[str, str] | None:
    """The country grammar has no candidate-name tier (no party to resolve)."""
    return None


def parse_country_extraction_label(label: str | None) -> tuple[str, str] | None:
    """(sentinel, country_slug) from a country-extraction factor's value_label.

    The label is the canonical JSON an ``extraction`` factor produces
    (llm/base.py) — ``{"country": "ukraine"}``. The ``country`` field is
    resolved through the same canonicaliser the patterns tier uses (slug,
    canonical name, or alias), so the LLM can return any of them. Unknown
    countries → None, never a guess.
    """
    if not label:
        return None
    try:
        payload = json.loads(label)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    slug = resolve_country(payload.get("country"))
    if slug is None:
        return None
    return _NO_SIDE, slug


def country_key_code(key: str) -> str | None:
    """Display/cache code for a country entity = the slug itself (URL-safe).

    The slug is already the canonical key used by the weights file, the region
    map, and the /war cache keys; the human-readable name is derived in the API
    layer via ``geo.country_name``. Returns None for an unrecognised slug so a
    bad key surfaces rather than silently coining a code.
    """
    return key if region_of(key) is not None else None


# ---------------------------------------------------------------------------
# Cycle membership gate (race_calendar bridge)
# ---------------------------------------------------------------------------


def allowed_states_for(group_by: str, cycle: int) -> frozenset[str] | None:
    """State CODES that have a race of this office on the ballot in ``cycle``.

    The second, entity-level cycle gate (the first is the per-market
    ``election_cycle`` drop): after entities are built, ``us_state`` (Senate) and
    ``us_governor`` keep only the states the ``race_calendar`` lists as contested
    that cycle — so a stray non-2026 market that slipped the per-market gate
    (undetermined cycle, kept fail-open) still can't coin a phantom state.

    Returns None where no calendar gate applies: ``us_house_district`` (all 435
    districts are up every even year — pass-through) and ``country`` (war).
    """
    if group_by == "us_state":
        return senate_states_for_cycle(cycle)
    if group_by == "us_governor":
        return governor_states_for_cycle(cycle)
    return None


# ---------------------------------------------------------------------------
# Hierarchical rollup parentage
# ---------------------------------------------------------------------------


def rollup_parent(group_by: str, key: str) -> tuple[str, str | None] | None:
    """(parent_key, parent_code) for an entity key one level up, or None if flat.

    ``us_house_district`` nests: the district key ``"<State>-<n>"``
    ("California-20", at-large "North Dakota-AL") folds up into its state
    ("California", code "CA"). ``country`` nests: a country slug folds up into
    its region ("ukraine" → "europe"). State-keyed grammars (``us_state`` /
    ``us_governor``) are already at the state level and return None — there is
    no sub-level to fold, so ``entity_lean_mean`` rolls them up flat.
    """
    if group_by == "us_house_district":
        state = key.rpartition("-")[0]
        return state, state_code(state)
    if group_by == "country":
        region = region_of(key)
        if region is None:
            return None
        return region, region
    return None


# ---------------------------------------------------------------------------
# Declarative dispatch
# ---------------------------------------------------------------------------


class _Grammar:
    """Per-group_by binding of parse callbacks to the classify/merge core."""

    def __init__(
        self,
        parse_direct: Callable[[str], tuple[str, str] | None],
        parse_candidate: Callable[[str], tuple[str, str] | None],
        parse_label: Callable[[str | None], tuple[str, str] | None],
        key_code: Callable[[str], str | None],
    ) -> None:
        self.parse_direct = parse_direct
        self.parse_candidate = parse_candidate
        self.parse_label = parse_label
        self.key_code = key_code


_GRAMMARS: dict[str, _Grammar] = {
    "us_state": _Grammar(
        parse_seat_race, parse_candidate_race, parse_extraction_label, state_code
    ),
    "us_house_district": _Grammar(
        parse_district_race,
        parse_district_candidate_race,
        parse_district_extraction_label,
        district_code,
    ),
    # us_governor reuses the state key space (one governorship per state) and
    # the senate label validator (the value_label is the same {state, party,
    # race_type} shape), differing only in the title grammar.
    "us_governor": _Grammar(
        parse_governor_race,
        parse_governor_candidate_race,
        parse_extraction_label,
        state_code,
    ),
    # Partyless war grammar: patterns = single-country alias match, no
    # candidates tier, llm_fallback = country-extraction factor.
    "country": _Grammar(
        parse_country_direct,
        _no_candidate,
        parse_country_extraction_label,
        country_key_code,
    ),
}


def parse_label_for(spec: ExtractionSpec) -> Callable[[str | None], tuple[str, str] | None]:
    """The value_label validator matching the spec's entity key space.

    Callers (aggregator, senate-board route) use this to turn the index's
    audited ``type: extraction`` factor evaluations into (party, key) hints.
    """
    return _GRAMMARS[spec.group_by].parse_label


# Office name for the curated candidate-party override table, per grammar.
_OFFICE_OF_GRAMMAR: dict[str, str | None] = {
    "us_state": "senate",
    "us_house_district": "house",
    "us_governor": "governor",
    "country": None,
}


def classify_entities(
    spec: ExtractionSpec,
    markets: list[tuple[int, str, float]],
    party_by_market: dict[int, str] | None = None,
    venue_by_market: dict[int, str | None] | None = None,
    extraction_by_market: dict[int, tuple[str, str]] | None = None,
    extraction_kinds: dict[int, str] | None = None,
) -> list[EntityObservation]:
    """Classify selected markets into entity observations per the spec.

    ``spec.methods`` gates the tiers: an absent method disables its parse
    entirely (e.g. ``methods: [patterns]`` for a lean index that doesn't
    want single-candidate prices in a mean). Tier precedence within an
    enabled set is fixed (patterns > candidates > llm_fallback) regardless
    of the YAML's list order. The hint maps mean the same as in
    :func:`seat_mapping.extract_contested_seats`.
    """
    grammar = _GRAMMARS[spec.group_by]
    methods = set(spec.methods)
    return classify_markets(
        markets,
        parse_direct=grammar.parse_direct if "patterns" in methods else None,
        parse_candidate=grammar.parse_candidate if "candidates" in methods else None,
        key_code=grammar.key_code,
        party_by_market=party_by_market,
        venue_by_market=venue_by_market,
        extraction_by_market=(
            extraction_by_market if "llm_fallback" in methods else None
        ),
        # An LLM-named CANDIDATE leg joins the candidate sum only where the
        # candidates tier is enabled; a patterns-only lean keeps it directed.
        extraction_kinds=(
            extraction_kinds
            if "llm_fallback" in methods and "candidates" in methods
            else None
        ),
        office=_OFFICE_OF_GRAMMAR.get(spec.group_by),
    )


def extract_entities(
    spec: ExtractionSpec,
    markets: list[tuple[int, str, float]],
    party_by_market: dict[int, str] | None = None,
    venue_by_market: dict[int, str | None] | None = None,
    extraction_by_market: dict[int, tuple[str, str]] | None = None,
) -> list[ContestedSeat]:
    """One representative directed probability per entity (Bernoulli view).

    :func:`classify_entities` + :func:`seat_mapping.merge_observations` —
    this is what ``formula: seat_projection_sum`` feeds the Poisson-binomial.
    For ``group_by: us_state`` with all methods enabled this is byte-identical
    to ``seat_mapping.extract_contested_seats``.
    """
    return merge_observations(
        classify_entities(
            spec,
            markets,
            party_by_market=party_by_market,
            venue_by_market=venue_by_market,
            extraction_by_market=extraction_by_market,
        )
    )
