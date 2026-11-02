"""Draw a stratified gold sample from the live corpus.

Why not just sample at random
-----------------------------
Two reasons, and the second is the one that bites.

1. **Prevalence.** A uniform draw of 300 from ~2.2 M markets contains roughly
   zero members of any given index. Every label would be a negative and the
   set would measure nothing.

2. **Blind spots.** Sampling from the selector's own output can only ever
   measure precision. The markets that matter for recall — genuine members the
   keyword regex never matched and the cosine floor culled — are by
   construction absent from that pool. ``near_miss`` is the stratum that goes
   looking for them, and it is the only reason this module needs the vector
   store at all.

Each stratum records its POPULATION alongside its sample size, because the
metrics are meaningless without the re-weighting (see ``metrics``).

Everything here reads; nothing is written to the DB. The output is a file.
"""

from __future__ import annotations

import random
from typing import Any

import structlog
from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.eval.schema import (
    STRATUM_RATIONALE,
    GoldItem,
    GoldSetMeta,
    GoldSpec,
    Stratum,
    StratumMeta,
    new_meta,
)
from pmi_core.eval.taxonomy import Taxonomy, resolve_taxonomy
from pmi_core.llm import embed_query
from pmi_core.models import CoreMarket, VecMarketEmbedding

log = structlog.get_logger(__name__)

DEFAULT_PER_STRATUM: dict[Stratum, int] = {
    "candidate_positive": 80,
    "candidate_zero": 80,
    "near_miss": 60,
    "model_disagreement": 50,
    "corpus_random": 30,
}

# Adjacency is a QUANTILE of the corpus, not an offset from the declared
# `min_similarity`. Absolute cosine offsets do not transfer between embedding
# models or anchors, and on the live corpus the offset version was not merely
# imprecise, it was vacuous: cosine against the BTC anchor is a narrow bell
# centred on 0.55 whose global minimum is ~0.39, so a `0.6 - 0.20 = 0.40` floor
# admitted 94 % of all markets and the "near miss" sample came back as baseball
# props and tennis handicaps.
#
# Top 3 % puts the floor at ~0.658 for this anchor, which is where the titles
# stop being noise: 0.60-0.65 is player total-bases and tweet counts, 0.65-0.70
# is other financial price thresholds (Fed funds, gold, S&P futures), 0.70+ is
# crypto/asset prices. A BTC market the selector missed plausibly lives in the
# top 3 %; it does not live at 0.61.
NEAR_MISS_QUANTILE = 0.03

# Cap on how much of the band to materialise. The band is a range predicate, so
# it cannot use the HNSW index at all (see `_near_miss`) — this bounds the work.
NEAR_MISS_SCAN_CAP = 20_000

# How many full market rows to hydrate out of the shuffled band. Comfortably
# above any per-stratum size so `sample_gold` can still de-duplicate against the
# earlier strata without running the band dry.
NEAR_MISS_FETCH = 400


async def _anchor_bands(
    session: AsyncSession, tax: Taxonomy
) -> list[tuple[str, float, list[float]]]:
    """``(anchor_text, adjacency_floor, embedding)`` per declared anchor.

    Embedded once and threaded through both strata that need it, so ``near_miss``
    and ``corpus_random`` are guaranteed to split the corpus on exactly the same
    predicate. Re-embedding per caller would be a correctness risk and not just
    waste: the two strata have to partition, and they cannot if one of them
    measures adjacency against a slightly different vector.

    The floor is derived from the corpus rather than from the index def, and the
    two are logged side by side because the gap between them is diagnostic. On
    the live BTC anchor the declared ``min_similarity`` is 0.6, the top-3 %
    quantile is ~0.658, and the threshold the selector *actually* enforces is
    0.788 — the cosine of the 10 000th global hit, because ``top_k`` saturates
    long before ``min_cosine`` binds.
    """
    model = settings.active_embedding_model
    policy = tax.universe_filters()
    out: list[tuple[str, float, list[float]]] = []
    for anchor in tax.anchors():
        vec = list(await embed_query(anchor.text, model=model))
        cos = (1.0 - VecMarketEmbedding.embedding.cosine_distance(vec)).label("c")
        deduped = (
            select(CoreMarket.id.label("market_id"), cos)
            .join(VecMarketEmbedding, VecMarketEmbedding.market_id == CoreMarket.id)
            .where(VecMarketEmbedding.model == model, *policy)
            .distinct(CoreMarket.id)
            .order_by(CoreMarket.id, cos.desc())
            .subquery()
        )
        floor = (
            await session.execute(
                select(
                    func.percentile_cont(1.0 - NEAR_MISS_QUANTILE)
                    .within_group(deduped.c.c.asc())
                    .label("floor")
                )
            )
        ).scalar_one_or_none()
        if floor is None:
            log.warning("eval.sampling.no_embeddings_for_anchor", anchor=anchor.text[:60])
            continue
        log.info(
            "eval.sampling.adjacency_floor",
            anchor=anchor.text[:60],
            quantile=NEAR_MISS_QUANTILE,
            floor=round(float(floor), 4),
            declared_min_similarity=anchor.declared_min_similarity,
        )
        out.append((anchor.text, float(floor), vec))
    return out


def _adjacent_clause(bands: list[tuple[str, float, list[float]]]) -> ColumnElement[bool] | None:
    """EXISTS predicate for "this market is semantically adjacent to the index".

    Correlated on ``CoreMarket.id`` so it can be negated for the residual stratum.
    """
    model = settings.active_embedding_model
    clauses = [
        select(1)
        .select_from(VecMarketEmbedding)
        .where(
            VecMarketEmbedding.market_id == CoreMarket.id,
            VecMarketEmbedding.model == model,
            (1.0 - VecMarketEmbedding.embedding.cosine_distance(vec)) >= low,
        )
        .exists()
        for _anchor, low, vec in bands
    ]
    if not clauses:
        return None
    return or_(*clauses)


def _to_item(market: Any, stratum: Stratum) -> GoldItem:
    return GoldItem(
        market_id=market.id,
        venue=market.venue,
        title=market.title or "",
        description=(market.description or None),
        stratum=stratum,
        # Frozen at sampling time. Volume rather than book depth: the
        # aggregator prefers depth and falls back to volume, but depth needs a
        # per-market LATERAL into a 126 M-row table and the gold set only needs
        # an error-severity ordering, not the exact production weight.
        liquidity=(
            float(market.last_volume_24h)
            if getattr(market, "last_volume_24h", None) is not None
            else None
        ),
        is_play_money=bool(getattr(market, "is_play_money", False)),
    )


async def _near_miss(
    session: AsyncSession,
    tax: Taxonomy,
    pool_ids: set[int],
    bands: list[tuple[str, float, list[float]]],
    rng: random.Random,
    seed: int,
) -> tuple[list[Any], int]:
    """Semantically adjacent markets the selector did NOT return.

    The only window onto false negatives, so its definition is deliberately
    "adjacent AND absent" rather than "in the band just under the floor". Two
    live findings forced that:

    * The semantic search is itself capped — ``top_k = min(max_markets*10,
      20_000)``, which on the BTC anchor saturated at exactly 10 000 matches.
      Markets above the 0.6 floor that do not make that cosine-ranked cut are
      never seen by the selector at all, and a band defined as *below* the floor
      excludes precisely those. They are false negatives with high similarity —
      the most damaging kind.
    * "Absent from the pool" also captures misses caused by the downstream
      filters, without having to enumerate which filter did it. ``systems.live``
      attributes the cause separately.

    Requires a semantic anchor — without one there is no similarity axis and the
    stratum is empty. Worth stating rather than papering over: a keyword-only
    index has no cheap way to look for its own false negatives.

    Why this does not use the ANN index
    -----------------------------------
    The obvious implementation — ``store.search(min_cosine=floor - band)`` —
    returned zero usable rows on the first live run. ANN yields the top-k by
    cosine DESCENDING, but this stratum wants a *range*, and well over 20 000
    markets sit above the BTC anchor's floor; the whole scan budget is spent
    above the bar and the interesting region is never reached. No top_k fixes it
    reliably because the count above the floor is unknown and unbounded.

    A range predicate cannot use HNSW at all, so cost is controlled from the
    other side: apply the taxonomy's cheap policy filters first, which cut
    ~1.9 M embeddings to the set this index could ever draw from, and compute
    cosine only over those survivors.

    ``ORDER BY random()`` rather than by cosine: this is a SAMPLE, and taking its
    top would bias every draw toward the highest-similarity misses. Postgres has
    to materialise the whole set to shuffle it anyway, which is what makes
    ``count(*) OVER ()`` an exact population rather than a lower bound.
    """
    if not bands:
        log.warning("eval.sampling.near_miss_skipped", reason="taxonomy declares no anchor")
        return [], 0

    model = settings.active_embedding_model
    policy = tax.universe_filters()
    await session.execute(text("SELECT setseed(:s)"), {"s": (seed % 1000) / 1000.0})
    band: dict[int, float] = {}
    population = 0
    for anchor, low, vec in bands:
        cosine = (1.0 - VecMarketEmbedding.embedding.cosine_distance(vec)).label("cosine")
        # DISTINCT ON the market, best cosine wins. A market can hold more than
        # one embedding row per model — a re-titled market keeps the old
        # text_sha256 alongside the new one (8 884 such rows live) — and a plain
        # join would count those markets twice in `count(*) OVER ()`, inflating
        # the population and therefore this stratum's weight.
        deduped = (
            select(CoreMarket.id.label("market_id"), cosine)
            .join(VecMarketEmbedding, VecMarketEmbedding.market_id == CoreMarket.id)
            .where(
                VecMarketEmbedding.model == model,
                cosine >= low,
                CoreMarket.id.not_in(pool_ids) if pool_ids else text("true"),
                *policy,
            )
            .distinct(CoreMarket.id)
            .order_by(CoreMarket.id, cosine.desc())
            .subquery()
        )
        rows = (
            await session.execute(
                select(
                    deduped.c.market_id,
                    deduped.c.cosine,
                    func.count().over().label("band_total"),
                )
                .order_by(func.random())
                .limit(NEAR_MISS_SCAN_CAP)
            )
        ).all()
        band_total = int(rows[0][2]) if rows else 0
        population = max(population, band_total)
        log.info(
            "eval.sampling.near_miss_band",
            anchor=anchor[:60],
            adjacency_floor=round(low, 3),
            population=band_total,
            materialised=len(rows),
        )
        for market_id, cos, _total in rows:
            band.setdefault(market_id, float(cos))

    if not band:
        log.info("eval.sampling.near_miss", band_hits=0, fetched=0)
        return [], 0

    # Only the sample needs full rows; fetching the whole band would pull tens of
    # thousands of descriptions for the ~60 that get used.
    ids = list(band)
    rng.shuffle(ids)
    head = ids[: max(NEAR_MISS_FETCH, 1)]
    markets = list(
        (await session.execute(select(CoreMarket).where(CoreMarket.id.in_(head)))).scalars().all()
    )
    rng.shuffle(markets)
    log.info(
        "eval.sampling.near_miss",
        band_hits=len(band),
        population=population,
        fetched=len(markets),
    )
    return markets, population


async def _model_disagreement(
    session: AsyncSession, spec: GoldSpec, rng: random.Random
) -> list[Any]:
    """Markets where two production models split on the membership question.

    The highest information-per-label stratum: on the live corpus the same
    factor bound to different models disagrees at scale, and every one of those
    markets is a place where the current system is provably wrong in one
    direction or the other.
    """
    stmt = text(
        """
        SELECT a.market_id
        FROM audit_evaluations a
        JOIN audit_evaluations b
          ON b.market_id = a.market_id
         AND b.factor_id = a.factor_id
         AND b.model_id <> a.model_id
        WHERE a.factor_id = :factor
          AND a.value_numeric IS NOT NULL
          AND b.value_numeric IS NOT NULL
          AND (a.value_numeric >= 0.5) <> (b.value_numeric >= 0.5)
        GROUP BY a.market_id
        """
    )
    ids = [r[0] for r in (await session.execute(stmt, {"factor": spec.membership_question})).all()]
    if not ids:
        return []
    markets = list(
        (
            await session.execute(select(CoreMarket).where(CoreMarket.id.in_(ids)))
        ).scalars().all()
    )
    rng.shuffle(markets)
    log.info("eval.sampling.model_disagreement", markets=len(markets))
    return markets


async def _corpus_random(
    session: AsyncSession,
    tax: Taxonomy,
    pool_ids: set[int],
    bands: list[tuple[str, float, list[float]]],
    n: int,
    seed: int,
) -> tuple[list[Any], int]:
    """Uniform draw from the RESIDUAL corpus — neither selected nor adjacent.

    Anchors the prevalence estimate. Two constraints make it the residual rather
    than the whole corpus, and both are correctness rather than taste:

    * It shares the taxonomy's policy universe with every other stratum. A corpus
      drawn from a wider universe than the candidate strata inflates its own
      population weight, and that weight already dominates by three orders of
      magnitude, so the error lands directly on population precision. (Live:
      dropping the quality gate and the open-market filter here inflated the
      universe from 86 k to 505 k — a 5.8× error in the heaviest weight.)
    * It excludes the pool and the adjacency set. Horvitz-Thompson re-weighting
      assumes the strata PARTITION the population; on the first live draw
      ``near_miss`` (73 554) was entirely contained in this stratum's universe
      (86 054), so 86 % of the corpus was being counted twice at two different
      weights. Subtracting them leaves three disjoint pieces: selected /
      adjacent-but-unselected / everything else.

    Postgres ``setseed`` makes the draw reproducible so a re-run of
    ``eval sample`` with the same seed returns the same markets and the gold file
    diff stays readable.
    """
    filters = tax.universe_filters()
    adjacent = _adjacent_clause(bands)
    if adjacent is not None:
        filters.append(~adjacent)
    if pool_ids:
        filters.append(CoreMarket.id.not_in(pool_ids))

    population = int(
        (await session.execute(select(func.count()).select_from(CoreMarket).where(*filters))).scalar_one()
    )
    await session.execute(text("SELECT setseed(:s)"), {"s": (seed % 1000) / 1000.0})
    markets = list(
        (
            await session.execute(
                select(CoreMarket).where(*filters).order_by(func.random()).limit(n)
            )
        ).scalars().all()
    )
    log.info("eval.sampling.corpus_random", population=population, drawn=len(markets))
    return markets, population


async def sample_gold(
    spec: GoldSpec,
    per_stratum: dict[str, int] | None = None,
    *,
    seed: int = 20260802,
    note: str = "",
) -> tuple[GoldSetMeta, list[GoldItem]]:
    """Draw one stratified sample. Returns unlabelled items — ``labeling`` fills them."""
    sizes = {**DEFAULT_PER_STRATUM, **(per_stratum or {})}
    rng = random.Random(seed)
    tax = resolve_taxonomy(spec)

    meta = new_meta(spec, note=note)
    items: list[GoldItem] = []
    chosen: set[int] = set()

    async with session_scope() as session:
        split = await tax.candidate_split(session)
        rng.shuffle(split.positive)
        rng.shuffle(split.zero)
        pool_ids = split.pool_ids
        pools: dict[Stratum, tuple[list[Any], int]] = {
            "candidate_positive": (split.positive, len(split.positive)),
            "candidate_zero": (split.zero, len(split.zero)),
        }

        bands = await _anchor_bands(session, tax)

        near, near_population = await _near_miss(session, tax, pool_ids, bands, rng, seed)
        pools["near_miss"] = (near, near_population)

        disagree = await _model_disagreement(session, spec, rng)
        pools["model_disagreement"] = (disagree, len(disagree))

        rand, rand_pop = await _corpus_random(
            session, tax, pool_ids, bands, sizes.get("corpus_random", 0), seed
        )
        pools["corpus_random"] = (rand, rand_pop)

        # Strata are filled in a fixed order and de-duplicated across them: a
        # market can legitimately qualify for several (a disagreement case is
        # usually also a candidate), and double-counting one item would break
        # the population weighting. First stratum wins, and the loser's
        # `sampled` count reflects what it actually contributed.
        for stratum in (
            "candidate_positive",
            "candidate_zero",
            "near_miss",
            "model_disagreement",
            "corpus_random",
        ):
            pool, population = pools.get(stratum, ([], 0))
            want = sizes.get(stratum, 0)
            taken = 0
            for market in pool:
                if taken >= want:
                    break
                if market.id in chosen:
                    continue
                chosen.add(market.id)
                items.append(_to_item(market, stratum))  # type: ignore[arg-type]
                taken += 1
            meta.strata[stratum] = StratumMeta(
                population=max(population, taken),
                sampled=taken,
                rationale=STRATUM_RATIONALE.get(stratum, ""),  # type: ignore[arg-type]
            )

    log.info(
        "eval.sampling.done",
        taxonomy=spec.taxonomy,
        items=len(items),
        strata={k: v.sampled for k, v in meta.strata.items()},
    )
    return meta, items


__all__ = ["DEFAULT_PER_STRATUM", "NEAR_MISS_QUANTILE", "sample_gold"]
