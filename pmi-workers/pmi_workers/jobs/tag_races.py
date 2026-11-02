"""tag-races — cache the keyword-path election classification into core_market_race_tag.

For each US election office we run the EXACT selection + classification the
aggregator's keyword path uses — ``select_markets_by_keyword`` →
``_extraction_inputs`` → ``classify_entities`` (all three tiers, incl.
``llm_fallback``) — and persist one row per resulting observation. Election
indexes then select their components with a plain ``WHERE office/cycle`` instead
of the keyword pool + id-DESC cap (which silently truncated older venues once
kalshi bloated the pool). Because the tags ARE the keyword path's own output,
tag-path scoring is byte-identical to the keyword path — minus the cap.

One canonical seats def drives each office; the MAGA leans share the same
extraction grammar + factors, so a single tag set serves both. Cycle is stored
per-market (NULL when election_cycle can't be determined — the selector
fail-opens those into the current cycle). Atomic full re-tag (DELETE + insert;
the table is small). Batch lane: a full scan, not a page-facing snapshot.
"""

from __future__ import annotations

import structlog

# _extraction_inputs is the aggregator's own keyword-path hint/triple builder;
# reusing it (rather than re-deriving) is what makes the tags byte-identical to
# what the keyword selector classified.
from pmi_core.db import session_scope
from pmi_core.dsl.ir import load_index_def
from pmi_core.engine.aggregator import MarketEvaluations, _extraction_inputs
from pmi_core.engine.election_cycle import election_cycle
from pmi_core.engine.extraction import classify_entities
from pmi_core.engine.pipeline import (
    INDEX_DEFS_DIR,
    ensure_factor_evaluations,
    ensure_index_definition,
    load_cached_evaluations,
    resolve_factor_models,
)
from pmi_core.engine.race_identity import exclusion_reason
from pmi_core.engine.selector import select_markets_by_keyword
from pmi_core.models import CoreMarket, CoreMarketRaceTag
from sqlalchemy import delete, insert, select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.tag_races")

# Bump when the classification grammar changes so a re-tag is detectable (rows
# with an older classifier_version are stale). v2 (2026-08-31): tags are produced
# by the aggregator's own keyword-path classification (_extraction_inputs +
# classify_entities, all three tiers) rather than a standalone resolver.
# v3 (2026-10-12, CORR-1.11): vote-share / polling / multi-race titles are
# excluded; an LLM-extracted CANDIDATE leg is tagged kind=candidate (summed by
# party) instead of party_direct; curated candidate-party overrides apply.
CLASSIFIER_VERSION = 3

# One canonical election def per office drives the classification. The MAGA lean
# indexes share the same extraction grammar + factors (republican_on_yes + the
# per-office extraction factor), so one tag set serves the seats AND the leans.
_OFFICE_DEFS: dict[str, str] = {
    "senate": "us-senate-2026-republican-seats",
    "house": "us-house-2026-republican-seats",
    "governor": "us-governor-2026-republican-seats",
}

# tag-races classifies the WHOLE keyword pool, uncapped — killing the treadmill
# is the point, so it must not inherit the def's id-DESC max_markets cap (the very
# truncation the tag path exists to remove). The pool is ~13k house legs at most,
# a trivial in-Python classify. Election defs carry no semantic selector, so the
# cap here is pure id-DESC and lifting it only widens coverage.
_UNCAPPED = 1_000_000


def _tag_kind(tier: str) -> str:
    """party_direct = a directed P(R) reading (patterns / llm_fallback);
    candidate = a candidate-name leg summed by party downstream — including one
    the LLM extraction named (its tier is already ``candidates``)."""
    return "party_direct" if tier in ("patterns", "llm_fallback") else "candidate"


def _state_district(group_by: str, key: str) -> tuple[str, int | None]:
    """('California', 20) for us_house_district 'California-20'; (key, None) else."""
    if group_by == "us_house_district":
        state, _, num = key.rpartition("-")
        return (state or key), (int(num) if num.isdigit() else None)
    return key, None


async def _price_by_market(session, ids: list[int]) -> dict[int, float]:
    """{market_id: last_price} for priced markets (the _extraction_inputs gate)."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(CoreMarket.id, CoreMarket.last_price).where(
                CoreMarket.id.in_(ids), CoreMarket.last_price.is_not(None)
            )
        )
    ).all()
    return {mid: float(p) for mid, p in rows}


def _observe(ir, spec, markets, evalmap, prices):
    """rows → `_extraction_inputs` → `classify_entities` → observations. No I/O;
    reused for both the cached pass and the post-bootstrap re-classify."""
    rows = [
        MarketEvaluations(
            market=m,
            by_factor={f.id: evalmap[(m.id, f.id)] for f in ir.factors if (m.id, f.id) in evalmap},
            last_price=prices.get(m.id),
            liquidity=None,  # classification doesn't use liquidity
        )
        for m in markets
    ]
    triples, party_hints, venue_by_market, extraction_hints, kinds = _extraction_inputs(
        rows, ir, spec
    )
    return classify_entities(
        spec,
        triples,
        party_by_market=party_hints,
        venue_by_market=venue_by_market,
        extraction_by_market=extraction_hints,
        extraction_kinds=kinds,
    )


async def _classify_office(session, office: str, index_id: str) -> list[dict]:
    """Run the keyword-path classification for one office → tag dicts (one per obs)."""
    ir, yaml_text, sha256 = load_index_def(INDEX_DEFS_DIR / f"{index_id}.yaml")
    spec = ir.extraction
    assert spec is not None  # the seats defs always declare an extraction stage

    ir.max_markets = _UNCAPPED  # classify the full pool, not the capped id-DESC slice
    markets = await select_markets_by_keyword(session, ir)
    ids = [m.id for m in markets]
    def_row = await ensure_index_definition(session, ir, yaml_text, sha256)
    _, resolved = await resolve_factor_models(session, ir)
    prices = await _price_by_market(session, ids)

    # Pass 1: classify with whatever's already cached. Party-direct titles resolve
    # via patterns (no LLM at all); candidate / llm_fallback tiers use cached
    # factor evals.
    evalmap = await load_cached_evaluations(session, ir, resolved, ids)
    observations = _observe(ir, spec, markets, evalmap, prices)

    # Self-bootstrap: a priced, non-noise market that classified into NOTHING is an
    # unresolved race — typically a brand-new venue/leg whose title no pattern
    # matches (e.g. IBKR "Wyoming Senate General Election — <candidate>"). Evaluate
    # the ONE shared universal extractor (seat_race_extraction) for JUST those, then
    # re-classify. Bounded to genuinely-new markets (not the whole pool); one shared
    # LLM for every venue; index-independent cache so scoring reuses it and it's
    # never re-billed. Breaks the migration's chicken-and-egg — scoring only ever
    # evaluated already-tagged markets, so a new market could never get judged.
    ext_id = next((f.id for f in ir.factors if f.type == "extraction"), None)
    if ext_id is not None:
        resolved_ids = {o.market_id for o in observations}
        unresolved = [
            m
            for m in markets
            if m.id not in resolved_ids
            and prices.get(m.id) is not None
            and not exclusion_reason(m.title, m.external_id)
        ]
        if unresolved:
            await ensure_factor_evaluations(
                session, ir, unresolved, resolved, def_row.id, factor_ids={ext_id}
            )
            evalmap = await load_cached_evaluations(session, ir, resolved, ids)
            observations = _observe(ir, spec, markets, evalmap, prices)

    market_by_id = {m.id: m for m in markets}
    out: list[dict] = []
    for o in observations:
        m = market_by_id.get(o.market_id)
        if m is None:
            continue
        state, district = _state_district(spec.group_by, o.key)
        out.append(
            {
                "market_id": o.market_id,
                "office": office,
                "state": state,
                "district": district,
                # NULL when undeterminable — the selector fail-opens these.
                "cycle": election_cycle(m.venue, m.title, m.external_id, m.closes_at),
                "party_side": o.party,
                # party_direct = a directed P(R) reading (patterns / llm_fallback);
                # candidate = a candidate-name leg summed by party downstream —
                # including one the LLM extraction named (resolved_via keeps
                # the provenance; the aggregator rebuilds the tier from kind).
                "kind": _tag_kind(o.tier),
                "resolved_via": o.resolved_via or o.tier,
                "classifier_version": CLASSIFIER_VERSION,
            }
        )
    return out


@register("tag-races", queue="batch")
async def run() -> dict:
    async with session_scope() as session:
        tags: list[dict] = []
        seen: set[int] = set()
        for office, index_id in _OFFICE_DEFS.items():
            for t in await _classify_office(session, office, index_id):
                # A market belongs to exactly one race; the office grammars are
                # mutually exclusive, but guard against a title both parsers claim.
                if t["market_id"] in seen:
                    continue
                seen.add(t["market_id"])
                tags.append(t)

        # Atomic full re-tag: replace wholesale rather than diff (table is small);
        # a market that stopped classifying simply loses its row.
        await session.execute(delete(CoreMarketRaceTag))
        if tags:
            await session.execute(insert(CoreMarketRaceTag), tags)

    by_office: dict[str, int] = {}
    for t in tags:
        by_office[t["office"]] = by_office.get(t["office"], 0) + 1
    log.info("tag_races.done", tagged=len(tags), by_office=by_office)
    return {"tagged": len(tags), "by_office": by_office}
