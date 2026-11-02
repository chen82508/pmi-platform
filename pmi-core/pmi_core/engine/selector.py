"""Selector: turn `IndexDef.selectors` into a list of `core_markets` rows.

Three selector kinds, unioned (a market matching ANY selector is included):

* **keyword**  — Postgres POSIX regex (`~*`) with `\\m...\\M` word boundaries
  (not substring ilike): `war` no longer matches `software`/`award`, `strike`
  no longer matches `Counter-Strike`. Multi-word terms match as a bounded
  phrase; terms are regex-escaped so punctuation is literal.
* **category** — exact match on `core_markets.category`.
* **semantic** — cosine search over `vec_market_embeddings` via the active
  embedding model (anchor embedded with the query prefix), keeping markets at or
  above `min_similarity`. Degrades gracefully: if embeddings aren't populated or
  the embed endpoint is down, the semantic clause contributes nothing rather
  than failing the whole selection (keyword/category still apply).

Discovery vs the Tier 0 gate
----------------------------
SemanticSelector *discovers* relevant markets (high bar, ~0.78). The Tier 0
pre-filter in the pipeline is a separate, lower cosine *floor* (~0.5) that culls
keyword/category false-positives before the factor LLM loop. Both read the same
embeddings via the same VectorStore.
"""

from __future__ import annotations

import re

import structlog
from sqlalchemy import BigInteger, func, literal, not_, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.config import settings
from pmi_core.dsl.ir import CategorySelector, IndexDef, KeywordSelector, SemanticSelector
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.llm import embed_query
from pmi_core.models import CoreMarket, CoreMarketRaceTag
from pmi_core.vectorstore import get_vector_store

log = structlog.get_logger(__name__)

# Which office a us_* extraction grammar scopes to — the bridge from an index's
# extraction.group_by to core_market_race_tag.office.
_GROUP_BY_OFFICE: dict[str, str] = {
    "us_state": "senate",
    "us_house_district": "house",
    "us_governor": "governor",
}


def _race_tag_office(ir: IndexDef) -> str | None:
    """The tag office for a cycle-scoped US election index, else None.

    Only these select via ``core_market_race_tag`` (WHERE office/cycle) instead of
    the keyword pool + id-DESC cap; every other index (war/btc/sp500/…) keeps the
    keyword/semantic selector.
    """
    spec = ir.extraction
    if spec is None or spec.cycle is None:
        return None
    return _GROUP_BY_OFFICE.get(spec.group_by)


async def _select_by_race_tag(
    session: AsyncSession, ir: IndexDef, office: str
) -> list[CoreMarket]:
    """Election-index membership straight from the persisted race tags.

    A plain ``JOIN core_market_race_tag WHERE office/cycle`` returns exactly the
    race legs — no keyword pool, no cap, no id-DESC truncation (the bug that
    silently dropped older venues once kalshi bloated the pool). The venue and
    play-money filters still apply so behaviour matches the def; the live-market
    gate is shared with every other read surface. The set is bounded by the real
    races (≤ ~2k house legs), so no cap is needed.
    """
    # Cycle fail-open (mirrors the aggregator's _apply_cycle_gates gate 1): a tag
    # whose election_cycle couldn't be determined (yearless title, no ticker year,
    # no closes_at) has cycle NULL — keep it for THIS cycle rather than dropping it
    # (fail-closed would silently lose kalshi's yearless legs). A tag with a KNOWN
    # different cycle is excluded. The per-entity race_calendar state gate is
    # applied downstream by the aggregator (_apply_cycle_gates gate 2), so it is
    # not repeated here.
    filters = [
        CoreMarketRaceTag.office == office,
        or_(
            CoreMarketRaceTag.cycle == ir.extraction.cycle,
            CoreMarketRaceTag.cycle.is_(None),
        ),
        *live_market_conditions(),
    ]
    if ir.venues is not None:
        filters.append(CoreMarket.venue.in_(ir.venues))
    if not ir.include_play_money:
        filters.append(CoreMarket.is_play_money.is_(False))
    stmt = (
        select(CoreMarket)
        .join(CoreMarketRaceTag, CoreMarketRaceTag.market_id == CoreMarket.id)
        .where(*filters)
    )
    return list((await session.execute(stmt)).scalars().all())


def _keyword_pattern(term: str) -> str:
    """Build a Postgres POSIX regex for a single keyword/phrase.

    `\\m` / `\\M` are Postgres word boundary anchors (start / end of word).
    `re.escape` neutralises any regex metacharacters inside the term so e.g.
    a future selector with `"u.s."` matches literally rather than as regex.
    """
    return rf"\m{re.escape(term)}\M"


async def _semantic_market_ids(ir: IndexDef, *, top_k: int) -> list[int]:
    """Market ids matched by any SemanticSelector, cosine-DESC (most relevant first).

    Fail-open: any error (no embeddings yet, embed endpoint down) logs a warning
    and yields no ids — keyword/category selection still stands.

    `top_k` bounds the ids per anchor: the result feeds a `id IN (...)` clause,
    and asyncpg hard-caps a statement at 32,767 bind params — with full
    cross-venue embeddings (~366k vectors) an uncapped 0.55-similarity anchor
    blows past that. Cosine-desc ordering means the cap keeps the strongest
    matches.

    Returns an ORDERED, de-duplicated list (not a set): select_markets uses the
    rank to fill the max_markets cap by war-relevance instead of recency — a
    play-money venue like Manifold streams thousands of new low-relevance markets
    that otherwise win an id-DESC cap and starve established country-war markets.
    """
    semantic = [s for s in ir.selectors if isinstance(s, SemanticSelector)]
    if not semantic:
        return []

    model = settings.active_embedding_model
    store = get_vector_store()
    ranked: list[int] = []
    seen: set[int] = set()
    for sel in semantic:
        try:
            anchor_vec = await embed_query(sel.anchor, model=model)
            hits = await store.search(
                query_embedding=anchor_vec,
                model=model,
                min_cosine=sel.min_similarity,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001 - graceful degradation by design
            log.warning(
                "selector.semantic_failed",
                index_id=ir.id,
                anchor=sel.anchor[:80],
                error=str(exc),
            )
            continue
        for h in hits:  # hits are cosine-DESC from the store
            if h.market_id not in seen:
                seen.add(h.market_id)
                ranked.append(h.market_id)
        log.info(
            "selector.semantic",
            index_id=ir.id,
            anchor=sel.anchor[:80],
            min_similarity=sel.min_similarity,
            matched=len(hits),
        )
    return ranked


async def select_markets(session: AsyncSession, ir: IndexDef) -> list[CoreMarket]:
    """Return distinct markets matching ANY selector (keyword / category / semantic).

    Cycle-scoped US election indexes select from the persisted race tags (one
    WHERE office/cycle, no keyword pool + id-DESC cap that truncated older
    venues); every other index uses the keyword/category/semantic union. The
    ``tag-races`` job builds those tags by running THIS keyword path's exact
    classification (:func:`select_markets_by_keyword` + the aggregator's
    ``_extraction_inputs`` + ``classify_entities``), so the two selection routes
    resolve to the same component set.
    """
    office = _race_tag_office(ir)
    if office is not None:
        return await _select_by_race_tag(session, ir, office)
    return await select_markets_by_keyword(session, ir)


async def select_markets_by_keyword(
    session: AsyncSession, ir: IndexDef
) -> list[CoreMarket]:
    """The keyword / category / semantic union (the non-tag selection path).

    Public so the ``tag-races`` job can drive an election def down the SAME
    keyword pool the aggregator historically classified, bypassing the tag
    early-return, to (re)build the tags.
    """
    title_clauses = []
    category_clauses = []
    kw_terms: list[str] = []
    kw_patterns: list[str] = []
    for sel in ir.selectors:
        if isinstance(sel, KeywordSelector):
            kw_terms.extend(sel.terms)
            kw_patterns.extend(sel.title_patterns)
        elif isinstance(sel, CategorySelector):
            category_clauses.append(CoreMarket.category == sel.polymarket_tag)
        # SemanticSelector handled out-of-band via the vector store (below).

    # Combine ALL keyword terms into ONE alternation regex: `(\mwar\M|\minvade\M|…)`.
    # A single ~* lets the pg_trgm GIN index do one bitmap scan; ~100 separate OR-ed
    # ~* clauses forced a seq scan (36M regex evals → ~30-50min). Same \m..\M forms.
    # `title_patterns` join the SAME alternation but raw (not \m..\M-wrapped, not
    # re.escaped) — for formats no literal term captures (see KeywordSelector).
    if kw_terms or kw_patterns:
        parts = [_keyword_pattern(t) for t in kw_terms] + list(kw_patterns)
        combined_kw = "(" + "|".join(parts) + ")"
        title_clauses.append(CoreMarket.title.op("~*")(combined_kw))

    # CORR-2.6: the cap is per-index (ir.max_markets) falling back to the
    # configurable global default. Computed up front because the semantic
    # search is bounded relative to it.
    limit = ir.max_markets or settings.selector_max_markets

    # 10× headroom over the final limit: the cosine cut runs over ALL embedded
    # venues (incl. ones outside ir.venues, e.g. manifold), so the main query's
    # venue/resolved filters thin the id set afterwards. Clamped well under
    # asyncpg's 32,767 bind-param ceiling.
    semantic_ids = await _semantic_market_ids(ir, top_k=min(limit * 10, 20_000))

    where_clauses = []
    if title_clauses:
        where_clauses.append(or_(*title_clauses))
    if category_clauses:
        where_clauses.append(or_(*category_clauses))
    if semantic_ids:
        where_clauses.append(CoreMarket.id.in_(semantic_ids))

    if not where_clauses:
        return []

    # Venue scope is OPTIONAL: `ir.venues is None` means no venue restriction —
    # membership is the selector union (+ the downstream Tier 0 gate), which is
    # venue-agnostic. An explicit list narrows for structural reasons only (e.g.
    # seat-count indexes).
    #
    # Play-money is excluded from membership by default (single money-type bit,
    # not a venue list): play-money venues carry ~100× the market count and would
    # swamp a count-based score even though each carries a neutral liquidity
    # weight. An index opts in with `include_play_money: true`.
    # Membership starts from the selector union AND the shared live-market gate
    # (resolved_at / closes_at / per-venue done-flags — see market_status). The
    # SAME gate backs every read/display surface so a market can't drop out of the
    # score yet linger on the page.
    filters = [or_(*where_clauses), *live_market_conditions()]
    if ir.venues is not None:
        filters.insert(0, CoreMarket.venue.in_(ir.venues))
    if not ir.include_play_money:
        filters.append(CoreMarket.is_play_money.is_(False))
    # NOTE: no volume gate on the score path. NULL/0-volume markets stay members
    # and get weight 0 in the aggregator (engine/liquidity.py + `_liquidity_weights`)
    # — shown/counted but not moving a liquidity-weighted score. `ir.min_volume_24h`
    # is retained as an IR field and still drives the markets-board display trim
    # (engine/markets_board_compute.py), which is a separate surface.
    # Negative keyword filter (ir.exclude_terms): drop any market whose title
    # word-boundary-matches an exclude term, on BOTH the keyword and semantic
    # paths, BEFORE the cap so it fills with genuine members. Same \\m..\\M
    # matching as the positive terms, combined into ONE alternation regex so the
    # planner runs a single scan (51 separate NOT clauses made this ~30× slower).
    # Empty list → no-op (default).
    if ir.exclude_terms:
        combined = "(" + "|".join(_keyword_pattern(t) for t in ir.exclude_terms) + ")"
        filters.append(not_(CoreMarket.title.op("~*")(combined)))
    # Cap ordering: by war-RELEVANCE (semantic cosine rank), not recency. The
    # semantic hits arrive cosine-DESC; array_position gives each its 1-based rank
    # so the max_markets cap keeps the most on-topic markets. Keyword-only matches
    # (not in the semantic set) sort after, by recency. Without a semantic
    # selector there's no cosine signal → unchanged id-DESC (every non-semantic
    # index, e.g. senate/maga/btc, behaves exactly as before). This is THE fix for
    # play-money breadth: Manifold streams thousands of fresh low-relevance markets
    # that won an id-DESC cap and starved established country-war markets.
    if semantic_ids:
        # Rank via unnest(ids) WITH ORDINALITY joined on id — a hash join, O(n),
        # vs array_position's O(n·m) per-row scan of a 20k-element array (~50min).
        ranked = (
            func.unnest(literal(semantic_ids, type_=ARRAY(BigInteger)))
            .table_valued("mid", with_ordinality="ord")
            .render_derived(with_types=False)
        )
        stmt = (
            select(CoreMarket)
            .outerjoin(ranked, ranked.c.mid == CoreMarket.id)
            .where(*filters)
            .order_by(func.coalesce(ranked.c.ord, 2_000_000_000), CoreMarket.id.desc())
            .limit(limit)
        )
    else:
        stmt = (
            select(CoreMarket).where(*filters).order_by(CoreMarket.id.desc()).limit(limit)
        )
    result = await session.execute(stmt)
    markets = list(result.scalars().all())
    if len(markets) >= limit:
        log.warning(
            "selector.limit_saturated",
            index_id=ir.id,
            limit=limit,
            venues=ir.venues,
            hint="markets beyond the cap are silently dropped — raise "
            "`max_markets` in the index YAML or PMI_SELECTOR_MAX_MARKETS",
        )
    return markets
