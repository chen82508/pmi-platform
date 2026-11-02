"""Cross-venue peer grouping for holdings display (Micah peer-group port).

Micah deduplicated "the same question listed on several exchanges" into peer
groups via embeddings (§15.9: embedding 角色 = peer-group 跨家去重). The
platform's holdings UI wants the same thing: one card per *question*, stacked
venue chips, summed volume — instead of N near-identical cards.

Mechanics: given the component markets of a score, pair any two markets from
DIFFERENT venues whose embeddings (same embedding model) clear a cosine
threshold, then union-find the pairs into groups. Same-venue pairs are never
edges — Polymarket's intentional date-ladder variants ("ceasefire by Oct 31"
vs "by Dec 31") embed nearly identically and must stay separate cards; two
venues listing the same question is exactly the signal we're after. (A pair
of same-venue markets can still land in one group transitively through a
third venue's market — that's accepted: all three are then one question.)

Markets without an embedding row stay singletons. Pure read path — nothing
is persisted; grouping is recomputed per /explain call (≤ a few hundred
components → one pgvector self-join, milliseconds).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Same-question-different-venue phrasings ("Russia x Ukraine ceasefire
# agreement by December 31, 2026?" vs "Russia-Ukraine ceasefire in 2026?")
# sit well above this on nomic-embed-text; unrelated geopolitics sits below.
DEFAULT_THRESHOLD = 0.90


def union_groups(ids: list[int], edges: list[tuple[int, int]]) -> dict[int, int]:
    """Union-find ``ids`` over ``edges`` → {market_id: group_id}.

    The group id is the smallest market id in the group, so singletons map to
    themselves and output is deterministic.
    """
    parent: dict[int, int] = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    for a, b in edges:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            # Smaller root wins so the group id is stable.
            if rb < ra:
                ra, rb = rb, ra
            parent[rb] = ra

    return {i: find(i) for i in ids}


async def group_peers(
    session: AsyncSession,
    market_ids: list[int],
    threshold: float = DEFAULT_THRESHOLD,
    cross_venue_only: bool = True,
) -> dict[int, int]:
    """Map each market to its peer-group id.

    Returns ``{market_id: group_id}`` covering every input id (markets with
    no embedding or no qualifying twin are their own group).

    ``cross_venue_only`` (default True) keeps the holdings-dedup semantics:
    only markets on *different* venues can be paired, so Polymarket's
    intentional date-ladder variants ("ceasefire by Oct 31" vs "by Dec 31")
    stay separate cards. Pass False to also pair same-venue near-duplicates —
    the war By-Question anchor view wants those folded (all the "Russia-Ukraine
    ceasefire" phrasings/deadlines across every venue become one question).
    """
    ids = sorted(set(market_ids))
    if len(ids) < 2:
        return {i: i for i in ids}

    # Latest embedding per market (the table is append-only; a re-embed adds
    # a row), then pairs over the cosine threshold. `<=>` is pgvector cosine
    # distance; embeddings must share a model to be comparable, and vectors of
    # differing dims never share a model. The venue join/filter is only needed
    # for the cross-venue-only mode.
    venue_join = (
        "JOIN core_markets ma ON ma.id = a.market_id "
        "JOIN core_markets mb ON mb.id = b.market_id"
        if cross_venue_only
        else ""
    )
    venue_cond = "AND ma.venue <> mb.venue" if cross_venue_only else ""
    rows = await session.execute(
        text(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (market_id, model)
                       market_id, model, embedding
                FROM vec_market_embeddings
                WHERE market_id = ANY(:ids)
                ORDER BY market_id, model, generated_at DESC
            )
            SELECT a.market_id, b.market_id
            FROM latest a
            JOIN latest b
              ON a.model = b.model AND a.market_id < b.market_id
            {venue_join}
            WHERE 1 - (a.embedding <=> b.embedding) >= :threshold
              {venue_cond}
            """
        ),
        {"ids": ids, "threshold": threshold},
    )
    edges = [(a, b) for a, b in rows.all()]
    return union_groups(ids, edges)
