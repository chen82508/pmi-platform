"""Reshape a ``geo_weighted_mean`` breakdown into ts_page_cache payloads (W7).

The war index is a single declarative def whose ``ts_index_scores.breakdown``
already carries the whole country→region→world hierarchy (unlike MAGA, which
composes three chamber indexes). So war composition is pure dict reshaping of
one breakdown into:

  * the national payload (``war:{cycle}``) — world heat + per-country map + per-
    region rows, for the choropleth and the by-region list;
  * one per-country detail payload (``war-country:{slug}:{cycle}``) — heat +
    the contributing contracts, for the country drill-down.

Pure-Python, no DB — unit-testable in isolation, called by the war-snapshot job.
"""

from __future__ import annotations

from collections import defaultdict

from pmi_core.venues import is_play_money_venue

# Fields carried per member contract when an anchor folds several contracts —
# enough for a "N contracts across M exchanges" summary and an expandable list,
# without duplicating the anchor-level facing fields.
_MEMBER_FIELDS = (
    "market_id",
    "title",
    "venue",
    "yes_pct",
    "volume_24h",
    "url",
    "country_slug",
    "country_name",
    "play_money",
)

# The deepest contracts surfaced at roster level (map hover panel, movers, and
# the daily summary's citation ranking) so the world/region pages need not
# fetch 47 per-country detail blobs (D4/D6).
_TOP_CONTRACT_FIELDS = ("market_id", "title", "venue", "yes_pct", "volume_24h", "url", "play_money")
# Below this 24h book a country/contract is "thin" — the design's THIN badge.
_THIN_VOL_24H = 10_000.0


def _is_play_money(c: dict) -> bool:
    """A contract's money type: the breakdown's flag when present (2026-10-12+),
    else derived from the venue so older breakdowns classify the same way."""
    flag = c.get("play_money")
    if flag is not None:
        return bool(flag)
    return is_play_money_venue(c.get("venue"))


def _real_volume(c: dict) -> float | None:
    """A contract's 24h volume in DOLLARS — None for play-money, whose "volume"
    is site credits and must never be summed into a dollar book (2026-10-12:
    the climate "Hottest year" card showed $39K of which $27K was Manifold mana)."""
    if _is_play_money(c):
        return None
    return c.get("volume_24h")


def refresh_prices(contracts: list[dict], price_by_id: dict[int, float]) -> None:
    """Overwrite each contract's ``yes_pct`` in place with the live price when
    ``price_by_id`` has one (prices are 0–1; ``yes_pct`` is 0–100).

    One map, applied to every feed a snapshot writes (national top contracts,
    per-country details, the question feed and its members), so the same
    contract shows ONE price across the site at a snapshot — before 2026-10-12
    only the question feed was refreshed and a contract read 81 on one page and
    91.5 on another (its hourly score-time price).
    """
    for c in contracts:
        p = price_by_id.get(c.get("market_id"))
        if p is not None:
            c["yes_pct"] = round(float(p) * 100.0, 1)


def _country_volume_and_top(contracts: list[dict]) -> tuple[float | None, list[dict]]:
    """(24h real-money book, top-3 contracts by 24h volume) for one country.

    Volume dedupes by ``market_id`` (the aggregator already keeps one row per
    (country, url), but guard so a repeated market can't double-count the book)
    and counts REAL money only — play-money "volume" is site credits, not
    dollars. ``None`` volume when no real-money contract carries one.
    """
    vol_by_mid: dict[int, float] = {}
    for c in contracts:
        mid = c.get("market_id")
        v = _real_volume(c)
        if mid is not None and v is not None:
            vol_by_mid[mid] = v
    volume_24h = round(sum(vol_by_mid.values()), 2) if vol_by_mid else None
    top = sorted(
        (c for c in contracts if c.get("market_id") is not None),
        key=lambda c: (_real_volume(c) or 0.0),
        reverse=True,
    )[:3]
    top_contracts = [
        {**{k: c.get(k) for k in _TOP_CONTRACT_FIELDS}, "play_money": _is_play_money(c)}
        for c in top
    ]
    return volume_24h, top_contracts


def _country_map(breakdown: dict) -> dict[str, dict]:
    """{slug: {slug, name, region, heat, n_markets, exchanges}} from entities.

    ``exchanges`` = the distinct venues the country's contributing contracts
    trade on, so the by-country PMI card can stack the exchange chips (the old
    Micah PMIRow look — "which exchanges back this index"). Region rows get the
    same treatment in ``compose_national``.
    """
    out: dict[str, dict] = {}
    for slug, e in (breakdown.get("entities") or {}).items():
        contracts = e.get("contracts", [])
        venues = sorted({c["venue"] for c in contracts if c.get("venue")})
        volume_24h, top_contracts = _country_volume_and_top(contracts)
        out[slug] = {
            "slug": slug,
            "name": e.get("name", slug),
            "region": e.get("region"),
            "heat": e["heat"],
            "n_markets": e.get("n_markets", 0),
            "exchanges": venues,
            "volume_24h": volume_24h,
            "thin": volume_24h is not None and volume_24h < _THIN_VOL_24H,
            "top_contracts": top_contracts,
        }
    return out


def _region_map(breakdown: dict) -> dict[str, dict]:
    """{region_id: {region, name, heat, n_entities, n_scored, weight}}."""
    out: dict[str, dict] = {}
    for rid, g in (breakdown.get("groups") or {}).items():
        out[rid] = {
            "region": rid,
            "name": g.get("name", rid),
            "heat": g["heat"],
            "n_entities": g.get("n_entities", 0),
            "n_scored": g.get("n_scored", 0),
            "weight": g.get("weight", 0.0),
        }
    return out


def compose_national(
    score: float | None,
    breakdown: dict | None,
    cycle_year: int,
    computed_at_iso: str,
    version: int | None = None,
) -> dict:
    """The ``war:{cycle}`` payload: world heat + country map + region rows.

    Market counting: a two-sided market (e.g. "Russia x Ukraine ceasefire?")
    is attributed to every matched country, so Σ per-country ``n_markets``
    counts it once per side. ``n_markets`` on the world/region rows is the
    DISTINCT market count (what a user reads as "contracts"); the per-side
    sum is kept as ``n_attributions`` for anyone reconciling against the
    per-country rows.

    ``version`` is the methodology (index-definition) version that produced
    the breakdown — carried so trend readers can tell a def bump from a
    market move.
    """
    breakdown = breakdown or {}
    countries = _country_map(breakdown)
    regions = _region_map(breakdown)

    # Per-region stat-card data: distinct venues, distinct contributing markets
    # and distinct holdings (unique contract URLs) across the region's countries
    # — for the region page's PMI Score / Contracts / Exchanges / Holdings cards.
    for rid, reg in regions.items():
        venues: set[str] = set()
        urls: set[str] = set()
        market_ids: set[int] = set()
        vol_by_mid: dict[int, float] = {}  # dedupe: a 2-sided market hits ≥1 country
        n_attributions = 0
        for e in (breakdown.get("entities") or {}).values():
            if e.get("region") != rid:
                continue
            n_attributions += e.get("n_markets", 0)
            for c in e.get("contracts", []):
                if c.get("venue"):
                    venues.add(c["venue"])
                if c.get("url"):
                    urls.add(c["url"])
                mid = c.get("market_id")
                if mid is not None:
                    market_ids.add(mid)
                    v = _real_volume(c)
                    if v is not None:
                        vol_by_mid[mid] = v
        reg["exchanges"] = sorted(venues)
        reg["n_markets"] = len(market_ids)
        reg["n_attributions"] = n_attributions
        reg["n_holdings"] = len(urls)
        reg["volume_24h"] = round(sum(vol_by_mid.values()), 2) if vol_by_mid else None

    # World distinct count + 24h book + venue volume split + concentration, all
    # deduped by market_id (a two-sided market hits several countries but counts
    # once). breakdown.venues is venue→COUNT; the liquidity snapshot needs
    # venue→VOLUME, so sum it here. Forward-only volume: older breakdowns carry
    # no per-contract volume_24h, so world_volume_24h is None until it accrues.
    # ``world_volume_24h`` is REAL money (dollars); ``venue_volume`` keeps every
    # venue's own figure, play-money lanes included, because the liquidity split
    # labels them (◦) rather than pretending they are dollars.
    vol_of: dict[int, float] = {}
    raw_vol_of: dict[int, float] = {}
    venue_of: dict[int, str] = {}
    for e in (breakdown.get("entities") or {}).values():
        for c in e.get("contracts", []):
            mid = c.get("market_id")
            if mid is None:
                continue
            v = _real_volume(c)
            if v is not None:
                vol_of[mid] = v
            raw = c.get("volume_24h")
            if raw is not None:
                raw_vol_of[mid] = raw
            if c.get("venue"):
                venue_of.setdefault(mid, c["venue"])
    world_market_ids = set(raw_vol_of) | {
        c["market_id"]
        for e in (breakdown.get("entities") or {}).values()
        for c in e.get("contracts", [])
        if c.get("market_id") is not None
    }
    world_volume_24h = round(sum(vol_of.values()), 2) if vol_of else None
    venue_vol: dict[str, float] = defaultdict(float)
    for mid, v in raw_vol_of.items():
        ven = venue_of.get(mid)
        if ven:
            venue_vol[ven] += v
    venue_volume = {
        k: round(v, 2)
        for k, v in sorted(venue_vol.items(), key=lambda kv: -kv[1])
    }
    # Concentration: share of the top-4 vs bottom-30 priced countries. The
    # denominator is Σ per-country book, NOT world_volume_24h — a two-sided
    # market (US×Iran) counts fully toward BOTH countries, so per-country volumes
    # overlap and their sum exceeds the deduped world total (dividing by the
    # latter yielded a >100% share). Against the country-attributed total the
    # shares are proper fractions. Gate on ≥8 priced countries (else None).
    country_vols = sorted(
        (c["volume_24h"] for c in countries.values() if c.get("volume_24h")),
        reverse=True,
    )
    concentration: dict | None = None
    country_total = sum(country_vols)
    if country_total and len(country_vols) >= 8:
        concentration = {
            "top4_share": round(sum(country_vols[:4]) / country_total, 4),
            "bottom30_share": round(sum(country_vols[-30:]) / country_total, 4),
            "n_countries_priced": len(country_vols),
        }

    exchanges = sorted((breakdown.get("venues") or {}).keys())
    return {
        "computed_at": computed_at_iso,
        "cycle_year": cycle_year,
        "version": version,
        "world_heat": round(score, 2) if score is not None else None,
        "n_countries": len(countries),
        "n_regions": len(regions),
        "n_markets": len(world_market_ids),
        "n_attributions": sum(c["n_markets"] for c in countries.values()),
        "exchanges": exchanges,
        "world_volume_24h": world_volume_24h,
        "venue_volume": venue_volume,
        "concentration": concentration,
        "countries": countries,
        "regions": regions,
    }


def compose_country_details(breakdown: dict | None, cycle_year: int) -> dict[str, dict]:
    """{slug: per-country detail payload (heat + contributing contracts)}.

    Contracts arrive already unique per (country, URL): the aggregator's
    ``_best_by_key_url`` keeps one representative answer per venue URL per
    country (Manifold MULTIPLE_CHOICE ingests each answer as its own market,
    all sharing the parent URL). That is the single dedup implementation — a
    second URL pass here (removed 2026-07-05) applied a different tie-break to
    already-deduped rows, i.e. two subtly disagreeing copies of the same rule.
    """
    breakdown = breakdown or {}
    out: dict[str, dict] = {}
    for slug, e in (breakdown.get("entities") or {}).items():
        out[slug] = {
            "slug": slug,
            "name": e.get("name", slug),
            "region": e.get("region"),
            "heat": e["heat"],
            "n_markets": e.get("n_markets", 0),
            "n_scoring": e.get("n_scoring"),
            "weighting": e.get("weighting"),
            "cycle_year": cycle_year,
            # Copies: the snapshot refreshes prices in place and the breakdown
            # row must stay the score-time record.
            "contracts": [
                {**c, "play_money": _is_play_money(c)} for c in e.get("contracts", [])
            ],
        }
    return out


def _rank_key(c: dict) -> tuple[float, float]:
    """Sort key: relevancy first, then 24h volume (both descending)."""
    return ((c.get("relevancy") or 0), (c.get("volume_24h") or 0))


def compose_questions(
    breakdown: dict | None,
    cycle_year: int,
    limit: int = 400,
    anchor_of: dict[int, int] | None = None,
    group_titles: dict[int, str] | None = None,
) -> dict:
    """The 'By Question' tab source: one entry per distinct war *question*.

    Each contributing contract is first deduped to one row (a contract counted
    for several countries appears once, attributed to its highest-relevancy
    country). Then, when ``anchor_of`` is supplied — a ``{market_id: anchor_id}``
    map — contracts sharing an anchor fold into one entry: the highest-relevancy
    contract is the facing row, the rest ride in ``members`` with ``n_contracts``
    / ``venues`` summarising the fold. War groups by country set (all China–Taiwan
    contracts → one anchor), so an anchor is a country conflict, not one phrasing;
    ``group_titles`` (``{anchor_id: title}``) then supplies the LLM-written
    question title for multi-country anchors, while single-country anchors keep
    their contract's own title. With ``anchor_of`` None the feed stays flat (one
    entry per contract) — backward compatible with older cache rows and callers.

    Sorted by relevancy then 24h volume; capped at ``limit`` with ``total`` /
    ``truncated`` so a reader can say "top 400 of N" rather than passing the cap
    off as the universe.
    """
    group_titles = group_titles or {}
    breakdown = breakdown or {}
    best: dict[int, dict] = {}
    for slug, e in (breakdown.get("entities") or {}).items():
        for c in e.get("contracts", []):
            mid = c.get("market_id")
            if mid is None:
                continue
            rel = c.get("relevancy") or 0
            prev = best.get(mid)
            if prev is not None and (prev.get("relevancy") or 0) >= rel:
                continue
            best[mid] = {
                "market_id": mid,
                "title": c.get("title"),
                "venue": c.get("venue"),
                "yes_pct": c.get("yes_pct"),
                "direction": c.get("direction"),
                "relevancy": rel,
                "volume_24h": c.get("volume_24h"),
                "url": c.get("url"),
                "play_money": _is_play_money(c),
                "country_slug": slug,
                "country_name": e.get("name", slug),
                "country_heat": e.get("heat"),
            }

    if anchor_of is None:
        items = sorted(best.values(), key=_rank_key, reverse=True)
        return {
            "questions": items[:limit],
            "total": len(items),
            "truncated": len(items) > limit,
        }

    # Fold near-duplicate contracts of the same question into anchor entries.
    groups: dict[int, list[dict]] = defaultdict(list)
    for mid, item in best.items():
        groups[anchor_of.get(mid, mid)].append(item)

    anchors: list[dict] = []
    for gid, members in groups.items():
        members.sort(key=_rank_key, reverse=True)
        rep = dict(members[0])  # highest-relevancy contract is the facing row
        rep["anchor_id"] = gid
        rep["n_contracts"] = len(members)
        rep["venues"] = sorted({m["venue"] for m in members if m.get("venue")})
        rep["members"] = [{k: m.get(k) for k in _MEMBER_FIELDS} for m in members]
        # A folded anchor gets the LLM-written question as its headline; the
        # facing contract's own wording rides along as ``facing_title`` so the
        # page can always show the exact question the price belongs to (an
        # LLM headline once asked "escalate?" over a ceasefire contract).
        if gid in group_titles and group_titles[gid] != rep.get("title"):
            rep["facing_title"] = rep.get("title")
            rep["title"] = group_titles[gid]
        anchors.append(rep)
    anchors.sort(key=_rank_key, reverse=True)
    return {
        "questions": anchors[:limit],
        "total": len(anchors),
        "truncated": len(anchors) > limit,
    }
