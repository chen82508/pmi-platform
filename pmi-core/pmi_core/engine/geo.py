"""Geographic resources for the war-index vertical (W1).

Ported from Micah legacy (the data package's constants + the job
executor's country weights) so the new platform's `group_by: country` extraction and
`cross_entity: gdp_pop_weighted` rollup reconcile against the legacy War Index site:

  * ``REGIONS`` / ``REGION_MAPPING`` — the 6-region taxonomy and the
    201-country → region map (legacy constants, verbatim).
  * ``country_weights.json`` — GDP + population per country (65 % GDP / 35 %
    population PMI weighting; ``engine/data/country_weights.json``, copied from
    the legacy job executor).
  * ``COUNTRY_ALIASES`` + :func:`match_country` — a hand-written alias table on
    top of the 201 canonical names, turning a market title into ONE country
    slug (the patterns tier of the country grammar). Per CLAUDE.md §15.9 the
    keyword/alias set is human-authored, NOT an LLM-generated pool.

The canonical key everywhere is the **slug** = ``name.lower().replace(" ", "-")``
— verified identical across the names list, the weights file, and the region
map. ``country_index.py``'s 65/35 within-group normalization is reproduced in
:func:`gdp_pop_weights`.

Pure-Python, no DB / network — unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Region taxonomy (legacy constants, verbatim)
# ---------------------------------------------------------------------------

REGIONS: list[dict[str, str]] = [
    {"id": "asia", "name": "Asia"},
    {"id": "europe", "name": "Europe"},
    {"id": "middle-east", "name": "Middle East"},
    {"id": "africa", "name": "Africa"},
    {"id": "americas", "name": "Americas"},
    {"id": "oceania", "name": "Oceania"},
]

REGION_NAME: dict[str, str] = {r["id"]: r["name"] for r in REGIONS}

# country slug → region id (201 countries; ported verbatim from legacy constants).
REGION_MAPPING: dict[str, str] = {
    # Asia (30)
    "afghanistan": "asia",
    "bangladesh": "asia",
    "bhutan": "asia",
    "brunei": "asia",
    "cambodia": "asia",
    "china": "asia",
    "india": "asia",
    "indonesia": "asia",
    "japan": "asia",
    "kazakhstan": "asia",
    "kyrgyzstan": "asia",
    "laos": "asia",
    "malaysia": "asia",
    "maldives": "asia",
    "mongolia": "asia",
    "myanmar": "asia",
    "nepal": "asia",
    "north-korea": "asia",
    "pakistan": "asia",
    "philippines": "asia",
    "singapore": "asia",
    "south-korea": "asia",
    "sri-lanka": "asia",
    "taiwan": "asia",
    "tajikistan": "asia",
    "thailand": "asia",
    "timor-leste": "asia",
    "turkmenistan": "asia",
    "uzbekistan": "asia",
    "vietnam": "asia",
    # Europe (52)
    "albania": "europe",
    "andorra": "europe",
    "armenia": "europe",
    "austria": "europe",
    "azerbaijan": "europe",
    "belarus": "europe",
    "belgium": "europe",
    "bosnia-and-herzegovina": "europe",
    "bulgaria": "europe",
    "croatia": "europe",
    "cyprus": "europe",
    "czechia": "europe",
    "denmark": "europe",
    "estonia": "europe",
    "faeroe-islands": "europe",
    "finland": "europe",
    "france": "europe",
    "georgia": "europe",
    "germany": "europe",
    "greece": "europe",
    "greenland": "europe",
    "holy-see": "europe",
    "hungary": "europe",
    "iceland": "europe",
    "ireland": "europe",
    "italy": "europe",
    "kosovo": "europe",
    "latvia": "europe",
    "liechtenstein": "europe",
    "lithuania": "europe",
    "luxembourg": "europe",
    "malta": "europe",
    "moldova": "europe",
    "monaco": "europe",
    "montenegro": "europe",
    "netherlands": "europe",
    "north-macedonia": "europe",
    "norway": "europe",
    "poland": "europe",
    "portugal": "europe",
    "romania": "europe",
    "russia": "europe",
    "san-marino": "europe",
    "serbia": "europe",
    "slovakia": "europe",
    "slovenia": "europe",
    "spain": "europe",
    "sweden": "europe",
    "switzerland": "europe",
    "turkey": "europe",
    "ukraine": "europe",
    "united-kingdom": "europe",
    # Middle East (16)
    "bahrain": "middle-east",
    "egypt": "middle-east",
    "gaza": "middle-east",
    "iran": "middle-east",
    "iraq": "middle-east",
    "israel": "middle-east",
    "jordan": "middle-east",
    "kuwait": "middle-east",
    "lebanon": "middle-east",
    "oman": "middle-east",
    "qatar": "middle-east",
    "saudi-arabia": "middle-east",
    "syria": "middle-east",
    "united-arab-emirates": "middle-east",
    "west-bank": "middle-east",
    "yemen": "middle-east",
    # Africa (54)
    "algeria": "africa",
    "angola": "africa",
    "benin": "africa",
    "botswana": "africa",
    "burkina-faso": "africa",
    "burundi": "africa",
    "cabo-verde": "africa",
    "cameroon": "africa",
    "central-african-republic": "africa",
    "chad": "africa",
    "comoros": "africa",
    "congo": "africa",
    "djibouti": "africa",
    "dr-congo": "africa",
    "equatorial-guinea": "africa",
    "eritrea": "africa",
    "eswatini": "africa",
    "ethiopia": "africa",
    "gabon": "africa",
    "gambia": "africa",
    "ghana": "africa",
    "guinea": "africa",
    "guinea-bissau": "africa",
    "ivory-coast": "africa",
    "kenya": "africa",
    "lesotho": "africa",
    "liberia": "africa",
    "libya": "africa",
    "madagascar": "africa",
    "malawi": "africa",
    "mali": "africa",
    "mauritania": "africa",
    "mauritius": "africa",
    "morocco": "africa",
    "mozambique": "africa",
    "namibia": "africa",
    "niger": "africa",
    "nigeria": "africa",
    "rwanda": "africa",
    "sao-tome-and-principe": "africa",
    "senegal": "africa",
    "seychelles": "africa",
    "sierra-leone": "africa",
    "somalia": "africa",
    "south-africa": "africa",
    "south-sudan": "africa",
    "sudan": "africa",
    "tanzania": "africa",
    "togo": "africa",
    "tunisia": "africa",
    "uganda": "africa",
    "zambia": "africa",
    "zimbabwe": "africa",
    # Americas (35)
    "antigua-and-barbuda": "americas",
    "argentina": "americas",
    "bahamas": "americas",
    "barbados": "americas",
    "belize": "americas",
    "bolivia": "americas",
    "brazil": "americas",
    "canada": "americas",
    "chile": "americas",
    "colombia": "americas",
    "costa-rica": "americas",
    "cuba": "americas",
    "dominica": "americas",
    "dominican-republic": "americas",
    "ecuador": "americas",
    "el-salvador": "americas",
    "grenada": "americas",
    "guatemala": "americas",
    "guyana": "americas",
    "haiti": "americas",
    "honduras": "americas",
    "jamaica": "americas",
    "mexico": "americas",
    "nicaragua": "americas",
    "panama": "americas",
    "paraguay": "americas",
    "peru": "americas",
    "saint-kitts-and-nevis": "americas",
    "saint-lucia": "americas",
    "saint-vincent-and-the-grenadines": "americas",
    "suriname": "americas",
    "trinidad-and-tobago": "americas",
    "united-states": "americas",
    "uruguay": "americas",
    "venezuela": "americas",
    # Oceania (15) — Solomon Islands moved here from Asia, Guam added
    "australia": "oceania",
    "fiji": "oceania",
    "guam": "oceania",
    "kiribati": "oceania",
    "marshall-islands": "oceania",
    "micronesia": "oceania",
    "nauru": "oceania",
    "new-zealand": "oceania",
    "palau": "oceania",
    "papua-new-guinea": "oceania",
    "samoa": "oceania",
    "solomon-islands": "oceania",
    "tonga": "oceania",
    "tuvalu": "oceania",
    "vanuatu": "oceania",
}

# Canonical display name per slug (inverse of the slug rule, with the few
# proper-noun casings the title-rule can't reproduce, e.g. "DR Congo").
_NAME_OVERRIDES: dict[str, str] = {
    "dr-congo": "DR Congo",
    "united-states": "United States",
    "united-kingdom": "United Kingdom",
    "united-arab-emirates": "United Arab Emirates",
    "north-korea": "North Korea",
    "south-korea": "South Korea",
    "north-macedonia": "North Macedonia",
    "south-africa": "South Africa",
    "south-sudan": "South Sudan",
    "bosnia-and-herzegovina": "Bosnia and Herzegovina",
    "central-african-republic": "Central African Republic",
    "costa-rica": "Costa Rica",
    "el-salvador": "El Salvador",
    "ivory-coast": "Ivory Coast",
    "saudi-arabia": "Saudi Arabia",
    "sri-lanka": "Sri Lanka",
    "west-bank": "West Bank",
    "papua-new-guinea": "Papua New Guinea",
    "new-zealand": "New Zealand",
    "holy-see": "Holy See",
    "san-marino": "San Marino",
    "cabo-verde": "Cabo Verde",
    "burkina-faso": "Burkina Faso",
    "guinea-bissau": "Guinea-Bissau",
    "timor-leste": "Timor-Leste",
    "faeroe-islands": "Faeroe Islands",
    "trinidad-and-tobago": "Trinidad and Tobago",
    "antigua-and-barbuda": "Antigua and Barbuda",
    "dominican-republic": "Dominican Republic",
    "marshall-islands": "Marshall Islands",
    "solomon-islands": "Solomon Islands",
    "saint-kitts-and-nevis": "Saint Kitts and Nevis",
    "saint-lucia": "Saint Lucia",
    "saint-vincent-and-the-grenadines": "Saint Vincent and the Grenadines",
    "sao-tome-and-principe": "Sao Tome and Principe",
    "equatorial-guinea": "Equatorial Guinea",
}


def slugify_country(name: str) -> str:
    """``name.lower().replace(" ", "-")`` — the canonical slug rule."""
    return name.strip().lower().replace(" ", "-")


def country_name(slug: str) -> str:
    """Display name for a country slug (Title-Case, with proper-noun overrides)."""
    return _NAME_OVERRIDES.get(slug, slug.replace("-", " ").title())


# ---------------------------------------------------------------------------
# GDP / population weights (legacy job-executor data, verbatim file)
# ---------------------------------------------------------------------------

GDP_WEIGHT = 0.65
POP_WEIGHT = 0.35

_WEIGHTS_PATH = Path(__file__).with_name("data") / "country_weights.json"


@lru_cache(maxsize=1)
def _weight_data() -> dict[str, tuple[float, float]]:
    """{slug: (gdp, population)} from the packaged JSON (module-level cache)."""
    raw = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
    out: dict[str, tuple[float, float]] = {}
    for slug, data in raw["countries"].items():
        if data is None:
            continue
        out[slug] = (float(data["gdp"]), float(data["population"]))
    return out


def gdp_pop_weights(slugs: list[str]) -> dict[str, float]:
    """Within-group 65 % GDP / 35 % population normalized weights.

    Reproduces the legacy job executor's ``compute_weighted_pmi``
    weight math (the numerator/denominator there is a weighted mean; this
    returns just the per-slug weight so the caller can fold values how it
    likes). A slug with no GDP/pop data gets the average weight of those that
    do (legacy "countries without weight data get the average weight"). When
    NONE of the slugs have data, every slug gets equal weight 1.0 (legacy's
    simple-average fallback).
    """
    data = _weight_data()
    with_data = [(s, data[s]) for s in slugs if s in data]
    without = [s for s in slugs if s not in data]

    if not with_data:
        return {s: 1.0 for s in slugs}

    total_gdp = sum(gdp for _, (gdp, _pop) in with_data)
    total_pop = sum(pop for _, (_gdp, pop) in with_data)
    weights: dict[str, float] = {}
    for s, (gdp, pop) in with_data:
        gdp_share = gdp / total_gdp if total_gdp > 0 else 0.0
        pop_share = pop / total_pop if total_pop > 0 else 0.0
        weights[s] = GDP_WEIGHT * gdp_share + POP_WEIGHT * pop_share
    if without:
        avg = sum(weights.values()) / len(weights)
        for s in without:
            weights[s] = avg
    return weights


# ---------------------------------------------------------------------------
# Region helpers
# ---------------------------------------------------------------------------


def region_of(slug: str) -> str | None:
    """Region id for a country slug, or None if unknown."""
    return REGION_MAPPING.get(slug)


@lru_cache(maxsize=1)
def _roster() -> dict[str, list[str]]:
    """{region_id: [country slugs in REGION_MAPPING]} — the full region roster."""
    out: dict[str, list[str]] = {r["id"]: [] for r in REGIONS}
    for slug, region in REGION_MAPPING.items():
        out.setdefault(region, []).append(slug)
    for region in out:
        out[region].sort()
    return out


def countries_in_region(region_id: str) -> list[str]:
    """Full country roster for a region (the include_zero denominator set)."""
    return list(_roster().get(region_id, []))


def region_mass(region_id: str) -> tuple[float, float]:
    """(total GDP, total population) of a region's full country roster."""
    data = _weight_data()
    gdp = pop = 0.0
    for slug in countries_in_region(region_id):
        if slug in data:
            g, p = data[slug]
            gdp += g
            pop += p
    return gdp, pop


def region_weights(region_ids: list[str]) -> dict[str, float]:
    """65 % GDP / 35 % population weights across regions (by full-roster mass).

    The world layer of ``geo_weighted_mean`` folds region scores by economic
    size: each region's mass is the GDP/pop sum of its entire roster (stable —
    independent of how many of its countries happened to be scored this tick),
    normalized across the participating regions. All regions carry data, so the
    only fallback (every mass 0) yields equal weights.
    """
    masses = {rid: region_mass(rid) for rid in region_ids}
    total_gdp = sum(g for g, _ in masses.values())
    total_pop = sum(p for _, p in masses.values())
    if total_gdp <= 0 and total_pop <= 0:
        return {rid: 1.0 for rid in region_ids}
    return {
        rid: GDP_WEIGHT * (g / total_gdp if total_gdp > 0 else 0.0)
        + POP_WEIGHT * (p / total_pop if total_pop > 0 else 0.0)
        for rid, (g, p) in masses.items()
    }


# ---------------------------------------------------------------------------
# Country alias table + title matcher (the patterns tier, human-authored)
# ---------------------------------------------------------------------------

# Hand-written aliases on top of the 201 canonical names. Maps a lowercase
# surface form → slug. Keep this human-curated (CLAUDE.md §15.9): do NOT grow
# it from an LLM keyword pool. Names themselves are added programmatically in
# _matchers(); this table is only for forms the canonical name doesn't cover.
COUNTRY_ALIASES: dict[str, str] = {
    "us": "united-states",
    "usa": "united-states",
    "u.s.": "united-states",
    "u.s.a.": "united-states",
    "america": "united-states",
    "american": "united-states",
    "united states of america": "united-states",
    "uk": "united-kingdom",
    "u.k.": "united-kingdom",
    "britain": "united-kingdom",
    "great britain": "united-kingdom",
    "british": "united-kingdom",
    "england": "united-kingdom",
    "uae": "united-arab-emirates",
    "emirates": "united-arab-emirates",
    "drc": "dr-congo",
    "d.r.c.": "dr-congo",
    "congo-kinshasa": "dr-congo",
    "democratic republic of the congo": "dr-congo",
    "democratic republic of congo": "dr-congo",
    "congo-brazzaville": "congo",
    "republic of the congo": "congo",
    "dprk": "north-korea",
    "rok": "south-korea",
    "prc": "china",
    "people's republic of china": "china",
    "russian federation": "russia",
    "russian": "russia",
    "burma": "myanmar",
    "ivory-coast": "ivory-coast",
    "cote d'ivoire": "ivory-coast",
    "côte d'ivoire": "ivory-coast",
    "czech republic": "czechia",
    "turkiye": "turkey",
    "türkiye": "turkey",
    "vatican": "holy-see",
    "vatican city": "holy-see",
    "swaziland": "eswatini",
    "cape verde": "cabo-verde",
    "east timor": "timor-leste",
    "palestine": "west-bank",
    "palestinian": "west-bank",
    "gaza strip": "gaza",
    "iranian": "iran",
    "israeli": "israel",
    "ukrainian": "ukraine",
    "chinese": "china",
    "taiwanese": "taiwan",
    "north korean": "north-korea",
    "south korean": "south-korea",
    # --- iter3 coverage broadening (2026-07-01): demonyms + capitals -----------
    # The semantic selector pulls ~7k war markets but geo only attributed ~11
    # countries, because match_countries keyed on the *literal canonical name*
    # only. Manifold/poly war titles overwhelmingly refer to a country by its
    # adjective ("Syrian civil war") or capital ("Will Kyiv fall?"). These add
    # those surfaces for the legacy-covered + war-prone set. Word-boundary
    # matching (\\m..\\M) + longest-first keep precision; n_fp is watched.
    # demonyms / adjectival forms
    "afghan": "afghanistan",
    "albanian": "albania",
    "argentine": "argentina",
    "argentinian": "argentina",
    "armenian": "armenia",
    "australian": "australia",
    "austrian": "austria",
    "azerbaijani": "azerbaijan",
    "azeri": "azerbaijan",
    "bahraini": "bahrain",
    "belarusian": "belarus",
    "belgian": "belgium",
    "bosnian": "bosnia-and-herzegovina",
    "bulgarian": "bulgaria",
    "canadian": "canada",
    "colombian": "colombia",
    "croatian": "croatia",
    "cuban": "cuba",
    "cypriot": "cyprus",
    "czech": "czechia",
    "danish": "denmark",
    "egyptian": "egypt",
    "estonian": "estonia",
    "ethiopian": "ethiopia",
    "finnish": "finland",
    "french": "france",
    "georgian": "georgia",
    "german": "germany",
    "greek": "greece",
    "hungarian": "hungary",
    "icelandic": "iceland",
    "indian": "india",
    "indonesian": "indonesia",
    "iraqi": "iraq",
    "irish": "ireland",
    "italian": "italy",
    "japanese": "japan",
    "jordanian": "jordan",
    "kazakh": "kazakhstan",
    "kuwaiti": "kuwait",
    "latvian": "latvia",
    "lebanese": "lebanon",
    "lithuanian": "lithuania",
    "malaysian": "malaysia",
    "mexican": "mexico",
    "moldovan": "moldova",
    "mongolian": "mongolia",
    "montenegrin": "montenegro",
    "burmese": "myanmar",
    "dutch": "netherlands",
    "norwegian": "norway",
    "omani": "oman",
    "pakistani": "pakistan",
    "filipino": "philippines",
    "philippine": "philippines",
    "polish": "poland",
    "portuguese": "portugal",
    "qatari": "qatar",
    "romanian": "romania",
    "saudi": "saudi-arabia",
    "serbian": "serbia",
    "singaporean": "singapore",
    "slovak": "slovakia",
    "slovenian": "slovenia",
    "somali": "somalia",
    "spanish": "spain",
    "sudanese": "sudan",
    "swedish": "sweden",
    "swiss": "switzerland",
    "syrian": "syria",
    "thai": "thailand",
    "turkish": "turkey",
    "venezuelan": "venezuela",
    "vietnamese": "vietnam",
    "yemeni": "yemen",
    "emirati": "united-arab-emirates",
    # capitals of war-prone / legacy-covered countries
    "kyiv": "ukraine",
    "kiev": "ukraine",
    "moscow": "russia",
    "tehran": "iran",
    "tel aviv": "israel",
    "damascus": "syria",
    "baghdad": "iraq",
    "kabul": "afghanistan",
    "sanaa": "yemen",
    "sana'a": "yemen",
    "khartoum": "sudan",
    "cairo": "egypt",
    "beirut": "lebanon",
    "riyadh": "saudi-arabia",
    "pyongyang": "north-korea",
    "taipei": "taiwan",
    "islamabad": "pakistan",
    "gaza city": "gaza",
    "ramallah": "west-bank",
    "ankara": "turkey",
    "doha": "qatar",
    "abu dhabi": "united-arab-emirates",
    "caracas": "venezuela",
    "havana": "cuba",
    "mogadishu": "somalia",
    "addis ababa": "ethiopia",
    "minsk": "belarus",
    "tbilisi": "georgia",
    "yerevan": "armenia",
    "baku": "azerbaijan",
    "astana": "kazakhstan",
}


@lru_cache(maxsize=1)
def _matchers() -> list[tuple[re.Pattern[str], str]]:
    """(word-boundary regex, slug) pairs, longest surface form first.

    Built from the canonical names (slug → "country name") plus the alias
    table. Sorted by surface length DESC so "United States" wins over a bare
    "States"-like fragment and "South Korea" over "Korea". Word boundaries
    (``\\b``) stop "Iran" from matching inside "Iranian" handled by an explicit
    alias, and "Niger" from matching inside "Nigeria" (longest-first + \\b).
    """
    surfaces: dict[str, str] = {}
    # Canonical names from the region map (every slug → its display name).
    for slug in REGION_MAPPING:
        surfaces[country_name(slug).lower()] = slug
    # Hand-written aliases (override / extend).
    for alias, slug in COUNTRY_ALIASES.items():
        surfaces[alias.lower()] = slug
    pairs: list[tuple[re.Pattern[str], str]] = []
    for surface in sorted(surfaces, key=len, reverse=True):
        slug = surfaces[surface]
        pat = re.compile(rf"(?<!\w){re.escape(surface)}(?!\w)", re.IGNORECASE)
        pairs.append((pat, slug))
    return pairs


def countries_in_title(title: str) -> list[str]:
    """All distinct country slugs mentioned in a title (match order preserved).

    Longest-surface-first matching, and once a span is consumed it can't be
    re-matched by a shorter contained surface (so "South Korea" is one hit, not
    also "Korea"). Order is by first appearance in the title.
    """
    spans: list[tuple[int, str]] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in claimed)

    for pat, slug in _matchers():
        for m in pat.finditer(title):
            if not overlaps(m.start(), m.end()):
                claimed.append((m.start(), m.end()))
                spans.append((m.start(), slug))
    seen: set[str] = set()
    out: list[str] = []
    for _pos, slug in sorted(spans):
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def resolve_country(value: object) -> str | None:
    """Canonical country slug for a slug / canonical name / alias, else None.

    Used to validate the ``country`` field an LLM country-extraction factor
    returns: accept a slug already in the region map, a name that slugs into
    one, or any single-country surface the alias matcher understands.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text in REGION_MAPPING:
        return text
    slug = slugify_country(text)
    if slug in REGION_MAPPING:
        return slug
    return match_country(text)


# Diplomatic-meeting markets name a country as the *venue*, not a war
# participant ("Will Trump and Putin meet next in Australia?"). When such a
# title mentions exactly ONE country, that country is incidental (the venue),
# so the patterns tier must not claim it (it crowned meeting venues as top war
# countries). The guard is applied single-country-only by the callers
# (:func:`match_country` is inherently single; :func:`match_countries` gates on
# ``len == 1``) — a two-party title like "peace talks between Russia and
# Ukraine" names war parties, not a venue, and "peace talks" is itself a war
# keyword, so the guard must not fire there.
_MEETING_VENUE_RE = re.compile(r"\b(meet|meeting|summit|talks)\b", re.IGNORECASE)


def match_country(title: str) -> str | None:
    """The single country a title is about, or None if 0/>1 or a meeting venue.

    High-precision patterns tier: claim ONLY when the title names exactly one
    country AND isn't a leaders'-meeting/summit market (where the country is the
    venue, not a war party). Used by :func:`resolve_country` to canonicalise a
    single LLM-returned country. The war patterns tier itself uses
    :func:`match_countries` (multi-country) — a war index is overwhelmingly
    bilateral, so dropping every >1-country title (the old behaviour) discarded
    the core of the index.
    """
    found = countries_in_title(title)
    if len(found) != 1:
        return None
    if _MEETING_VENUE_RE.search(title):
        return None
    return found[0]


def match_countries(title: str) -> list[str]:
    """Every country a title is about (the war patterns tier), in title order.

    Unlike :func:`match_country`, a bilateral title ("Russia invade Ukraine",
    "Iran strikes Israel") is attributed to BOTH countries — the legacy war
    index does the same (its per-country keyword matching is many-to-many), and
    the index's direction/relevancy factors apply equally to each participant
    (an escalation market escalates for both sides).

    The meeting-venue guard applies ONLY to single-country titles. There the
    lone country is the venue ("Will Putin and Zelenskyy meet next in Turkey?"
    — the leaders are people, so Turkey is the only country matched). A title
    naming TWO+ countries is a bilateral market where the countries are the war
    parties, not a venue — "formal peace talks between Russia and Ukraine",
    "Russia-Ukraine ceasefire". The old unconditional guard dropped those too,
    discarding real war markets on the "talks"/"summit" token (44 bilateral
    Russia-Ukraine peace/ceasefire markets in a 14-day sample). "peace talks"
    is itself a war keyword, so the guard must not fire on a two-party title.
    """
    found = countries_in_title(title)
    if not found:
        return []
    if len(found) == 1 and _MEETING_VENUE_RE.search(title):
        return []
    return found
