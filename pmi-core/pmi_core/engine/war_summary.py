"""Deterministic daily war summaries (D6) — templated prose + contract
citations over the enriched ``war:{cycle}`` snapshot payload.

The ask ``_derived_facts`` philosophy, per the §F decision (2026-09-14,
deterministic templates): every comparison, ranking and share is computed in
Python and the template only words it. No LLM anywhere, no authored fallback
copy — a fact the payload cannot support is OMITTED, never invented, so a
pre-enrichment cache row simply yields a shorter summary (or none at all).

Pure dict work — unit-testable in isolation; the ``war-summary`` job feeds it
the latest payload once a day and stashes the result in ts_page_cache.
"""

from __future__ import annotations

from pmi_core.venues import PLAY_MONEY_VENUES

# Mirrors the site's war-band vocabulary (pmi-web lib/micah/war-geo.ts
# `warBand`) — keep the two in step if the thresholds ever move.
_BANDS = ((70.0, "severe"), (50.0, "elevated"), (30.0, "simmering"), (15.0, "low"), (0.0, "calm"))

_CITATION_FIELDS = ("market_id", "title", "venue", "yes_pct", "volume_24h", "url")


def _band(heat: float) -> str:
    for floor, label in _BANDS:
        if heat >= floor:
            return label
    return "calm"


def _money(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${round(v)}"


def _dir(delta: float) -> str:
    return "up" if delta > 0 else "down"


def _move_clauses(d24: float | None, d7: float | None) -> str:
    """", up 0.4 over the last day and down 2.9 over the week" — whichever
    windows the payload actually carries (|Δ| < 0.05 reads as flat)."""
    parts: list[str] = []
    if d24 is not None:
        parts.append(
            f"{_dir(d24)} {abs(d24):.1f} over the last day" if abs(d24) >= 0.05 else "flat over the last day"
        )
    if d7 is not None:
        parts.append(
            f"{_dir(d7)} {abs(d7):.1f} over the week" if abs(d7) >= 0.05 else "flat over the week"
        )
    return ", " + " and ".join(parts) if parts else ""


def _citation(contract: dict, role: str) -> dict:
    row = {k: contract.get(k) for k in _CITATION_FIELDS}
    row["role"] = role
    return row


def _cite_countries(candidates: list[tuple[dict | None, str]], limit: int = 3) -> list[dict]:
    """Up to ``limit`` cited contracts — each candidate is (country row, role);
    the country's deepest listed contract with a real URL is the citation.
    Deduped by market id, first role wins."""
    out: list[dict] = []
    seen: set = set()
    for country, role in candidates:
        if country is None or len(out) >= limit:
            continue
        top = next(
            (c for c in country.get("top_contracts") or [] if c.get("url") and c.get("market_id") not in seen),
            None,
        )
        if top is None:
            continue
        seen.add(top.get("market_id"))
        out.append(_citation(top, role))
    return out


def _breadth_sentence(breadth: dict | None) -> str | None:
    if not breadth:
        return None
    up, down, flat = breadth.get("up"), breadth.get("down"), breadth.get("flat")
    if up is None or down is None or flat is None:
        return None
    return f"Across the book, {up} countries priced higher over the week, {down} lower and {flat} unchanged."


def compose_world_summary(payload: dict) -> dict | None:
    """The world page's daily note: ≤3 paragraphs + ≤3 cited contracts.

    Facts used (all straight off the enriched payload): world reading + band,
    Δ24h/Δ7d, 7d breadth, the largest weekly country move, the regional
    high/low, the real-money 24h book (play-money venues excluded from the
    dollar figure, matching the page's liquidity snapshot) and the top-4
    country concentration share.
    """
    heat = payload.get("world_heat")
    if heat is None:
        return None
    countries = list((payload.get("countries") or {}).values())
    regions = list((payload.get("regions") or {}).values())

    paragraphs: list[str] = []
    paragraphs.append(
        f"The world reading stands at {heat:.1f} — {_band(float(heat))} on the 0–100 scale"
        f"{_move_clauses(payload.get('world_delta_24h'), payload.get('world_delta_7d'))}."
    )

    p2: list[str] = []
    breadth = _breadth_sentence(payload.get("breadth_7d"))
    if breadth:
        p2.append(breadth)
    movers = [c for c in countries if c.get("delta_7d") is not None and abs(c["delta_7d"]) >= 0.05]
    mover = max(movers, key=lambda c: abs(c["delta_7d"]), default=None)
    if mover is not None:
        p2.append(
            f"The largest move is {mover['name']}, {_dir(mover['delta_7d'])} "
            f"{abs(mover['delta_7d']):.1f} to {mover['heat']:.1f}."
        )
    if len(regions) >= 2:
        hi = max(regions, key=lambda r: r["heat"])
        lo = min(regions, key=lambda r: r["heat"])
        p2.append(
            f"{hi['name']} carries the highest regional reading at {hi['heat']:.1f}; "
            f"{lo['name']} the lowest at {lo['heat']:.1f}."
        )
    if p2:
        paragraphs.append(" ".join(p2))

    p3: list[str] = []
    venue_volume = payload.get("venue_volume") or {}
    real_book = sum(v for k, v in venue_volume.items() if k not in PLAY_MONEY_VENUES)
    if real_book > 0:
        p3.append(f"The real-money 24-hour book behind the reading is {_money(real_book)}.")
    conc = payload.get("concentration") or {}
    if conc.get("top4_share") is not None:
        p3.append(
            f"The four deepest country books carry {round(conc['top4_share'] * 100)}% "
            f"of the country-attributed volume."
        )
    if p3:
        paragraphs.append(" ".join(p3))

    deepest = max(
        (c for c in countries if c.get("volume_24h")), key=lambda c: c["volume_24h"], default=None
    )
    hottest = max(countries, key=lambda c: c["heat"], default=None)
    citations = _cite_countries(
        [(deepest, "deepest book"), (hottest, "highest reading"), (mover, "biggest weekly move")]
    )
    return {"paragraphs": paragraphs, "citations": citations}


def compose_region_summary(payload: dict, region_id: str) -> dict | None:
    """One region's daily note: rank among regions, its own move, breadth and
    the largest mover inside it, its share of the region-attributed book, plus
    ≤2 cited contracts (deepest book / biggest move in the region)."""
    regions = payload.get("regions") or {}
    region = regions.get(region_id)
    if region is None or region.get("heat") is None:
        return None
    heat = float(region["heat"])
    ranked = sorted(regions.values(), key=lambda r: -r["heat"])
    rank = next((i + 1 for i, r in enumerate(ranked) if r.get("region") == region_id), None)

    paragraphs: list[str] = []
    rank_clause = f" — {rank}{_ordinal(rank)} of the {len(ranked)} regions" if rank else ""
    paragraphs.append(
        f"{region['name']} reads {heat:.1f} ({_band(heat)}){rank_clause}"
        f"{_move_clauses(region.get('delta_24h'), region.get('delta_7d'))}."
    )

    countries = [
        c for c in (payload.get("countries") or {}).values() if c.get("region") == region_id
    ]
    p2: list[str] = []
    with_d7 = [c for c in countries if c.get("delta_7d") is not None]
    if with_d7:
        up = sum(1 for c in with_d7 if c["delta_7d"] > 0.05)
        down = sum(1 for c in with_d7 if c["delta_7d"] < -0.05)
        flat = len(with_d7) - up - down
        p2.append(
            f"Within the region, {up} countries priced higher over the week, "
            f"{down} lower and {flat} unchanged."
        )
    movers = [c for c in with_d7 if abs(c["delta_7d"]) >= 0.05]
    mover = max(movers, key=lambda c: abs(c["delta_7d"]), default=None)
    if mover is not None:
        p2.append(
            f"The largest move is {mover['name']}, {_dir(mover['delta_7d'])} "
            f"{abs(mover['delta_7d']):.1f} to {mover['heat']:.1f}."
        )
    if p2:
        paragraphs.append(" ".join(p2))

    p3: list[str] = []
    vol = region.get("volume_24h")
    total = sum(r.get("volume_24h") or 0 for r in regions.values())
    if vol and total:
        p3.append(
            f"The region's 24-hour book is {_money(vol)} — "
            f"{round(vol / total * 100)}% of the region-attributed conflict volume."
        )
    if p3:
        paragraphs.append(" ".join(p3))

    deepest = max((c for c in countries if c.get("volume_24h")), key=lambda c: c["volume_24h"], default=None)
    citations = _cite_countries([(deepest, "deepest book"), (mover, "biggest weekly move")], limit=2)
    return {"paragraphs": paragraphs, "citations": citations}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
