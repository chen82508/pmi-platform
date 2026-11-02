"""war-snapshot job — pre-compute war heat into ts_page_cache (JSONB).

Runs every 10 minutes (crontab: `*/10 * * * *`), mirroring maga-snapshot.

Source = the YAML engine. Reads the latest persisted ``ts_index_scores`` for
``war-index-2026`` (``formula: geo_weighted_mean``) — whose breakdown already
carries the full country→region→world hierarchy — and reshapes it (via
``engine.war_compose``) into the ``ts_page_cache`` rows the ``/war/*`` endpoints
serve. Pure dict work over one persisted breakdown: a single indexed score read
plus the cache writes, no re-scoring.

Cache keys:
  * ``war:{cycle}`` — world heat + per-country map + per-region rows
  * ``war-country:{slug}:{cycle}`` — per-country heat + contributing contracts

Trend history: ts_page_cache IS the trend store — each run appends a row and the
trend endpoints read past rows' heat over time (same pattern as MAGA).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

import structlog
from pmi_core.db import session_scope
from pmi_core.engine import trend_store
from pmi_core.engine.anchor_title import (
    generate_anchor_title,
    polarity_matches,
    question_tokens,
)
from pmi_core.engine.geo import country_name
from pmi_core.engine.peer_grouping import group_peers, union_groups
from pmi_core.engine.war_compose import (
    compose_country_details,
    compose_national,
    compose_questions,
    refresh_prices,
)
from pmi_core.models import CoreIndexDefinition, CoreMarket, TsIndexScore, TsPageCache
from sqlalchemy import insert, select

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.war_snapshot")

CYCLE_YEAR = 2026
WAR_INDEX_ID = "war-index-2026"


async def _latest_score(
    session,
) -> tuple[float | None, dict | None, int | None, int | None]:
    """(score, breakdown, score_row_id, def_version) from the newest score of
    the war index, or (None, None, None, None)."""
    row = (
        await session.execute(
            select(
                TsIndexScore.score,
                TsIndexScore.breakdown,
                TsIndexScore.id,
                CoreIndexDefinition.version,
            )
            .join(
                CoreIndexDefinition,
                TsIndexScore.index_definition_id == CoreIndexDefinition.id,
            )
            .where(
                CoreIndexDefinition.index_id == WAR_INDEX_ID,
                CoreIndexDefinition.is_current.is_(True),
            )
            .order_by(TsIndexScore.as_of.desc())
            .limit(1)
        )
    ).first()
    if row:
        return (
            float(row.score) if row.score is not None else None,
            row.breakdown,
            row.id,
            row.version,
        )
    return None, None, None, None


# An anchor key is (country set, semantic group): contracts fold into one
# question only when they are about the SAME countries AND the same question.
_AnchorKey = tuple[tuple[str, ...], int]


async def _anchor_groups(
    session, breakdown: dict
) -> tuple[dict[int, int], dict[_AnchorKey, list[int]], dict[_AnchorKey, int]]:
    """Group markets by (country set, semantic group) — the By-Question anchor.

    Two keys compose so neither over- nor under-folds:

      * **country set** — a hard partition (from the breakdown's multi-country
        attribution). "US strike N countries" and "Israel strike N countries"
        never merge even though they read almost identically, because the
        country sets differ.
      * **semantic group** — within a country set, contracts about the same
        question fold. Two signals are unioned: an embedding fold
        (``group_peers``, cross-venue) that catches paraphrases, AND a
        question-token fold (``question_tokens``) that catches what embeddings
        miss — a Manifold umbrella prefix poisons the vector ("… — China
        invades Taiwan" sits at cosine 0.72 to a clean "Will China invade
        Taiwan…"), but both reduce to the token set {china, invade, taiwan}.
        So the "US strike 8…15 countries" ladder is one anchor, its nuclear-
        test markets another, and a prop's "China invades Taiwan" joins the
        clean invasion anchor.

    Returns ``anchor_of`` ({market_id: repr}, repr = group's min market id — a
    stable URL id), ``members`` ({anchor_key: [market_ids]}), and ``key_repr``
    ({anchor_key: repr}).
    """
    mc: dict[int, set[str]] = defaultdict(set)
    title_of: dict[int, str] = {}
    for slug, e in (breakdown.get("entities") or {}).items():
        for c in e.get("contracts", []):
            mid = c.get("market_id")
            if mid is not None:
                mc[mid].add(slug)
                title_of.setdefault(mid, c.get("title") or "")
    all_mids = sorted(mc)
    emb_of = await group_peers(session, all_mids, cross_venue_only=False)

    # Union the embedding groups with the question-token groups: two markets are
    # the same question if the embedding paired them OR their title token sets
    # match. The country set (in the anchor key below) keeps this from merging
    # e.g. a US strike ladder with an Israel one even when their tokens coincide.
    tok_of = {mid: question_tokens(title_of[mid]) for mid in all_mids}
    by_emb: dict[int, list[int]] = defaultdict(list)
    by_country: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for mid in all_mids:
        by_emb[emb_of.get(mid, mid)].append(mid)
        by_country[tuple(sorted(mc[mid]))].append(mid)
    edges: list[tuple[int, int]] = []
    for grp in by_emb.values():
        edges.extend((grp[0], m) for m in grp[1:])
    # Token fold: within one country set, two questions fold if one's content-word
    # set is a SUBSET of the other's (both >=3 tokens). This handles wording noise
    # embeddings miss — "invade Taiwan" ⊆ "invade the main island of Taiwan" ⊆ a
    # prop's "China starts a physical invasion of Taiwan" all collapse, regardless
    # of extra modifier words — while a genuinely different verb ("blockade") is
    # never a subset of "invade", so those stay separate. The >=3 floor stops a
    # bare {china, taiwan} (an action-less title) from swallowing everything.
    for mids in by_country.values():
        for i in range(len(mids)):
            ti = tok_of[mids[i]]
            if len(ti) < 3:
                continue
            for j in range(i + 1, len(mids)):
                tj = tok_of[mids[j]]
                if len(tj) >= 3 and (ti <= tj or tj <= ti):
                    edges.append((mids[i], mids[j]))
    sem_of = union_groups(all_mids, edges)

    members: dict[_AnchorKey, list[int]] = defaultdict(list)
    for mid in all_mids:
        akey: _AnchorKey = (tuple(sorted(mc[mid])), sem_of[mid])
        members[akey].append(mid)

    anchor_of: dict[int, int] = {}
    key_repr: dict[_AnchorKey, int] = {}
    for akey, mids in members.items():
        repr_id = min(mids)
        key_repr[akey] = repr_id
        for mid in mids:
            anchor_of[mid] = repr_id
    return anchor_of, members, key_repr


def _group_sample_titles(breakdown: dict, market_ids: set[int]) -> list[str]:
    """Up to 6 highest-relevancy contract titles in a group — LLM grounding.
    The first is the facing contract (compose_questions fronts the same one)."""
    items: list[tuple[float, str | None]] = []
    seen: set[int] = set()
    for _slug, e in (breakdown.get("entities") or {}).items():
        for c in e.get("contracts", []):
            mid = c.get("market_id")
            if mid in market_ids and mid not in seen:
                seen.add(mid)
                items.append(((c.get("relevancy") or 0), c.get("title")))
    items.sort(key=lambda x: x[0], reverse=True)
    return [t for _rel, t in items[:6] if t]


def _facing_direction(breakdown: dict, market_ids: set[int]) -> int | None:
    """The audited ``direction`` of the group's facing (highest-relevancy)
    contract — the polarity an LLM headline must keep."""
    best: tuple[float, int | None] | None = None
    for _slug, e in (breakdown.get("entities") or {}).items():
        for c in e.get("contracts", []):
            if c.get("market_id") in market_ids:
                rel = c.get("relevancy") or 0
                if best is None or rel > best[0]:
                    best = (rel, c.get("direction"))
    return None if best is None else best[1]


async def _read_anchor_titles(session, cycle_year: int) -> dict[str, str]:
    """The cached ``{anchor_key: LLM title}`` map (persists across snapshots)."""
    payload = await session.scalar(
        select(TsPageCache.payload)
        .where(TsPageCache.cache_key == f"war-anchor-titles:{cycle_year}")
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )
    return dict(payload) if payload else {}


async def _resolve_anchors(
    session, breakdown: dict, cycle_year: int, computed_at
) -> tuple[dict[int, int], dict[int, str]]:
    """(country set, semantic) anchors + an LLM question title per folded anchor.

    Any anchor folding 2+ contracts gets an LLM-written yes/no question (single-
    country ladders included); a lone contract keeps its own title. Titles are
    cached by anchor key, so only newly-seen anchors call the LLM — stable,
    no repeat cost. A cached or fresh headline is used only when its polarity
    agrees with the facing contract's audited direction (a "Will X escalate?"
    headline over a ceasefire contract is dropped, every run, so a bad cached
    title never resurfaces); the anchor then keeps the contract's own wording.
    Returns ``(anchor_of, group_titles)`` for compose_questions.
    """
    anchor_of, members, key_repr = await _anchor_groups(session, breakdown)
    title_map = await _read_anchor_titles(session, cycle_year)
    group_titles: dict[int, str] = {}
    new_titles = False
    rejected = 0
    for akey, mids in members.items():
        if len(mids) < 2:
            continue  # lone contract keeps its own title
        countries, emb = akey
        cache_key = f"{','.join(countries)}|{emb}"
        if cache_key not in title_map:
            names = [country_name(s) for s in countries] or ["the conflict"]
            title_map[cache_key] = await generate_anchor_title(
                names, _group_sample_titles(breakdown, set(mids))
            )
            new_titles = True
        title = title_map[cache_key]
        if not polarity_matches(title, _facing_direction(breakdown, set(mids))):
            rejected += 1
            continue  # headline contradicts the facing contract → its own title
        group_titles[key_repr[akey]] = title
    if rejected:
        log.info("war_snapshot.anchor_title_polarity_rejected", n=rejected)
    if new_titles:
        await session.execute(
            insert(TsPageCache).values(
                cache_key=f"war-anchor-titles:{cycle_year}",
                payload=title_map,
                computed_at=computed_at,
            )
        )
    return anchor_of, group_titles


async def _last_snapshot_source_id(session, cycle_year: int) -> int | None:
    """source_score_id of the newest existing ``war:{cycle}`` cache row."""
    payload = await session.scalar(
        select(TsPageCache.payload)
        .where(TsPageCache.cache_key == f"war:{cycle_year}")
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )
    return (payload or {}).get("source_score_id")


async def _payload_before(session, cache_key: str, cutoff: datetime) -> dict | None:
    """Freshest cache payload at or before ``cutoff`` — the delta baseline."""
    return await session.scalar(
        select(TsPageCache.payload)
        .where(
            TsPageCache.cache_key == cache_key,
            TsPageCache.computed_at <= cutoff,
        )
        .order_by(TsPageCache.computed_at.desc())
        .limit(1)
    )


def _delta(cur: float | None, old: float | None) -> float | None:
    return round(cur - old, 2) if (cur is not None and old is not None) else None


async def _inject_deltas(
    session, national: dict, cycle_year: int, computed_at: datetime
) -> None:
    """Bake Δ24h/Δ7d for world + each country + each region into ``national``.

    Diffs the whole heat map against the freshest ``war:{cycle}`` payload ≥24h /
    ≥7d ago — two cache reads, not 47 per-entity ``scalar_points`` queries. Heat
    history predates these fields, so heat deltas populate immediately; per-
    country breadth (up/down/flat over 7d) is derived in the same pass.
    """
    key = f"war:{cycle_year}"
    prev24 = await _payload_before(session, key, computed_at - timedelta(hours=24)) or {}
    prev7d = await _payload_before(session, key, computed_at - timedelta(days=7)) or {}

    national["world_delta_24h"] = _delta(
        national.get("world_heat"), prev24.get("world_heat")
    )
    national["world_delta_7d"] = _delta(
        national.get("world_heat"), prev7d.get("world_heat")
    )

    c24, c7 = prev24.get("countries") or {}, prev7d.get("countries") or {}
    up = down = flat = 0
    for slug, row in national.get("countries", {}).items():
        heat = row.get("heat")
        # Thin books (24h < $10k — the THIN badge) reprice on single trades, so a
        # Δ there is noise, not a view change (2026-10-13 report #4/#8: Turkey
        # "7d +73.5" on a near-empty book). Suppress it — the card still shows the
        # heat + THIN badge — which also drops thin countries from the breadth
        # count and the top-mover lists.
        if row.get("thin"):
            row["delta_24h"] = None
            row["delta_7d"] = None
            continue
        row["delta_24h"] = _delta(heat, (c24.get(slug) or {}).get("heat"))
        d7 = _delta(heat, (c7.get(slug) or {}).get("heat"))
        row["delta_7d"] = d7
        if d7 is not None:
            up += d7 > 0.05
            down += d7 < -0.05
            flat += -0.05 <= d7 <= 0.05
    national["breadth_7d"] = {"up": up, "down": down, "flat": flat}

    r24, r7 = prev24.get("regions") or {}, prev7d.get("regions") or {}
    for rid, row in national.get("regions", {}).items():
        heat = row.get("heat")
        row["delta_24h"] = _delta(heat, (r24.get(rid) or {}).get("heat"))
        row["delta_7d"] = _delta(heat, (r7.get(rid) or {}).get("heat"))


@register("war-snapshot", queue="snapshot")
async def run(cycle_year: int = CYCLE_YEAR) -> dict:
    """Compose ts_page_cache from the war-index-2026 breakdown (no re-scoring)."""
    computed_at = datetime.now(UTC)

    async with session_scope() as session:
        score, breakdown, score_id, version = await _latest_score(session)
        if breakdown is None:
            log.warning("war_snapshot.no_scores")
            return {"countries": 0, "regions": 0}

        # The job runs */10 but the source score updates hourly — without this
        # check ~5 of every 6 runs would append byte-identical payload rows
        # (the bulk of ts_page_cache growth). Same source row → nothing to do.
        if score_id is not None and score_id == await _last_snapshot_source_id(
            session, cycle_year
        ):
            log.info("war_snapshot.unchanged", source_score_id=score_id)
            return {"countries": 0, "regions": 0, "skipped": "unchanged"}

        # ONE live price map for every feed this snapshot writes, so a contract
        # shows the same price on the map hover, its country page, the question
        # feed and the question page. The hourly score-time price stays in the
        # breakdown row (the reading's own record); the feeds carry the price as
        # of this snapshot, stamped ``prices_as_of``.
        _all_mids = {
            c["market_id"]
            for e in (breakdown.get("entities") or {}).values()
            for c in e.get("contracts", [])
            if c.get("market_id") is not None
        }
        _px: dict[int, float] = {}
        if _all_mids:
            _px = dict(
                (
                    await session.execute(
                        select(CoreMarket.id, CoreMarket.last_price).where(
                            CoreMarket.id.in_(list(_all_mids)),
                            CoreMarket.last_price.isnot(None),
                        )
                    )
                ).all()
            )

        national = compose_national(
            score, breakdown, cycle_year, computed_at.isoformat(), version=version
        )
        national["source_score_id"] = score_id
        national["prices_as_of"] = computed_at.isoformat()
        for row in national.get("countries", {}).values():
            refresh_prices(row.get("top_contracts") or [], _px)
        # Δ24h/Δ7d per world/country/region + 7d breadth, from this cache key's
        # own history (D4). Writing world_volume_24h each tick also starts the
        # forward-only world/region volume series (D5) — retention keeps war:*.
        await _inject_deltas(session, national, cycle_year, computed_at)
        details = compose_country_details(breakdown, cycle_year)
        for payload in details.values():
            payload["prices_as_of"] = computed_at.isoformat()
            refresh_prices(payload.get("contracts") or [], _px)

        war_key = f"war:{cycle_year}"
        await session.execute(
            insert(TsPageCache).values(
                cache_key=war_key,
                payload=national,
                computed_at=computed_at,
            )
        )
        # Slim trend companion (trend_store): the country/region/world trend
        # endpoints read this instead of detoasting the full national board blob
        # once per snapshot across a 90-day window.
        if (slim := trend_store.slim_values(war_key, national, computed_at)) is not None:
            await session.execute(insert(TsPageCache).values(**slim))
        country_rows = [
            {
                "cache_key": f"war-country:{slug}:{cycle_year}",
                "payload": payload,
                "computed_at": computed_at,
            }
            for slug, payload in details.items()
        ]
        if country_rows:
            await session.execute(insert(TsPageCache), country_rows)

        # Anchor the By-Question feed by (country set, semantic group): contracts
        # about the same countries AND the same question fold into one anchor with
        # an LLM-written question title (a China–Taiwan invasion; a "US strike N
        # countries" ladder). A lone contract stays its own question.
        anchor_of, group_titles = await _resolve_anchors(
            session, breakdown, cycle_year, computed_at
        )
        # Country-set folding lands ~430 entries (61 multi + ~369 single); lift
        # the cap above that so the whole By-Question feed shows, not a top-400.
        questions = compose_questions(
            breakdown,
            cycle_year,
            limit=600,
            anchor_of=anchor_of,
            group_titles=group_titles,
        )
        # Same live price map as the national + country feeds (see above), so the
        # question tile agrees with the country page and its own live trend.
        _qs = questions.get("questions", [])
        refresh_prices(_qs, _px)
        for q in _qs:
            refresh_prices(q.get("members", []), _px)
        await session.execute(
            insert(TsPageCache).values(
                cache_key=f"war-questions:{cycle_year}",
                payload={
                    "cycle_year": cycle_year,
                    "computed_at": computed_at.isoformat(),
                    "prices_as_of": computed_at.isoformat(),
                    **questions,  # questions / total / truncated
                },
                computed_at=computed_at,
            )
        )

        await session.commit()

    log.info(
        "war_snapshot.done",
        source="yaml",
        countries=national["n_countries"],
        regions=national["n_regions"],
        world_heat=national["world_heat"],
        computed_at=computed_at.isoformat(),
    )
    return {"countries": national["n_countries"], "regions": national["n_regions"]}
