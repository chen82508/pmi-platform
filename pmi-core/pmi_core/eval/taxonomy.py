"""What a gold set is *about*, reduced to the five questions the harness asks.

The first taxonomy was BTC, which is a declarative index def, so the sampler
talked to an ``IndexDef`` directly: the policy universe came off the IR, the
adjacency axis came off its ``type: semantic`` selector, and "what does the
running system believe about this market" was a join against
``audit_evaluations``. That is a perfectly good description of the four indexes
that have YAML defs and LLM factors.

It is not a description of the National Economy board, which is the taxonomy
most worth measuring. GDP has no YAML def, no factor, and not one
``audit_evaluations`` row: membership is a regex chain in
``engine/gdp_board.py`` whose junk blacklist contains the literals ``3-3-3``
and ``nate silver``. That list grew by someone reading bad output and adding a
pattern — the same trial-and-error process that took the war def's
``exclude_terms`` to 51 entries — and nothing has ever measured what it rejects
by accident.

So the five things the harness needs are an interface, and a taxonomy is free
to answer them from a YAML def, from a regex chain, or from anything else:

``universe_filters``   the POLICY universe (open, unresolved, tradeable). Every
                       stratum draws from it, and they must all draw from the
                       *same* one or the population weights stop partitioning.
``anchors``            the semantic axes used to find near misses. Empty means
                       the taxonomy has no cheap way to look for its own false
                       negatives, and ``near_miss`` comes back empty — worth
                       stating loudly rather than silently degrading.
``candidate_split``    what the running system believes TODAY, split into what
                       it counts and what it rejected.
``factor_for``         the rubric a question is labelled against.
``predictions``        the system under test, per market, with diagnostics that
                       attribute a miss to the stage that caused it.

Deliberately NOT in the interface: anything about how the system is
implemented. ``IndexDefTaxonomy`` runs a real selector and reads cached LLM
judgments; ``GdpBoardTaxonomy`` runs four regexes. The metrics cannot tell, and
that is the point — it is what lets the two be compared on the same footing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import structlog
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.dsl.ir import FactorSpec, IndexDef, SemanticSelector, load_index_def
from pmi_core.engine import gdp_board as gb
from pmi_core.engine.market_status import live_market_conditions
from pmi_core.engine.pipeline import INDEX_DEFS_DIR, _tier0_prefilter
from pmi_core.engine.selector import select_markets
from pmi_core.eval.schema import GoldSpec
from pmi_core.models import AuditEvaluation, CoreMarket

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Anchor:
    """One axis of semantic adjacency.

    ``declared_min_similarity`` is carried alongside the text purely so the
    sampler can log it next to the floor it derives from the corpus. The gap
    between the two is a finding, not decoration: on the BTC anchor the def
    declares 0.6, the top-3 % quantile is 0.658, and the threshold the selector
    actually enforces is ~0.788 — because ``top_k`` saturates before
    ``min_cosine`` ever binds. None for taxonomies that declare no threshold.
    """

    text: str
    declared_min_similarity: float | None = None


@dataclass
class CandidateSplit:
    """The running system's current verdict over its own candidate pool.

    ``pool_ids`` is wider than ``positive + zero``: a market the system looked
    at but has no verdict for yet (selected but never sent to the factor) sits
    in neither list. Keeping it out of both is deliberate — it has no current
    prediction to be right or wrong about, and filing it under either would
    describe the sampler rather than the system.
    """

    positive: list[Any] = field(default_factory=list)
    zero: list[Any] = field(default_factory=list)
    pool_ids: set[int] = field(default_factory=set)


class Taxonomy(ABC):
    def __init__(self, spec: GoldSpec) -> None:
        self.spec = spec

    @abstractmethod
    def universe_filters(self) -> list[ColumnElement[bool]]:
        """Membership POLICY with every relevance criterion removed."""

    @abstractmethod
    def anchors(self) -> list[Anchor]:
        """The adjacency axes. Empty = no near_miss stratum, i.e. no view of FNs."""

    @abstractmethod
    async def candidate_split(self, session: AsyncSession) -> CandidateSplit:
        """Split the system's own candidate pool by what it decided."""

    @abstractmethod
    def factor_for(self, question: str) -> FactorSpec:
        """The rubric + answer type a labeller should use for ``question``."""

    @abstractmethod
    async def predictions(
        self, session: AsyncSession, market_ids: list[int]
    ) -> tuple[dict[int, float], dict]:
        """Membership per market plus a stage-attributed diagnostic breakdown."""


# ──────────────────────────────────────────────────────────────────────────
# Declarative index defs (BTC, S&P 500, war, …)
# ──────────────────────────────────────────────────────────────────────────


class IndexDefTaxonomy(Taxonomy):
    """A taxonomy backed by a YAML index def and its LLM factors."""

    @cached_property
    def ir(self) -> IndexDef:
        if not self.spec.index_id:
            raise ValueError(f"{self.spec.taxonomy} declares no index_id")
        ir, _yaml, _sha = load_index_def(INDEX_DEFS_DIR / f"{self.spec.index_id}.yaml")
        return ir

    def universe_filters(self) -> list[ColumnElement[bool]]:
        """Mirrors the non-relevance filters in ``engine.selector.select_markets``.

        Venue scope, the play-money bit, unresolved, still-open, the untraded
        quality gate. Getting this wrong is not cosmetic: ``corpus_random``
        supplies the prevalence anchor, and if it draws from a wider universe
        than the candidate strata its population weight over-counts and the
        population-corrected precision is simply wrong. (Live, on BTC: dropping
        the quality gate and the open-market filter inflated the universe from
        86 k to 505 k — a 5.8x error in the heaviest weight.)

        ``exclude_terms`` is deliberately NOT mirrored. A hand-written blacklist
        is itself a classification rule that can be too broad, so leaving it out
        is precisely what lets a gold set catch it over-rejecting.
        """
        ir = self.ir
        venues = (
            list(self.spec.venues) if self.spec.venues else (list(ir.venues) if ir.venues else None)
        )
        filters: list[ColumnElement[bool]] = [*live_market_conditions()]
        if venues:
            filters.append(CoreMarket.venue.in_(venues))
        if not ir.include_play_money:
            filters.append(CoreMarket.is_play_money.is_(False))
        if ir.min_volume_24h is not None:
            filters.append(CoreMarket.last_volume_24h >= ir.min_volume_24h)
        return filters

    def anchors(self) -> list[Anchor]:
        if self.spec.anchors:
            return [Anchor(text=a) for a in self.spec.anchors]
        return [
            Anchor(text=s.anchor, declared_min_similarity=s.min_similarity)
            for s in self.ir.selectors
            if isinstance(s, SemanticSelector)
        ]

    async def candidate_split(self, session: AsyncSession) -> CandidateSplit:
        """Split the selector's live output by what the production factors decided.

        Membership is read from ``audit_evaluations`` whatever model produced
        the row — i.e. by what the running system believes today, not by what a
        preferred binding would say.
        """
        markets = await select_markets(session, self.ir)
        pool_ids = {m.id for m in markets}
        if not markets:
            return CandidateSplit(pool_ids=pool_ids)

        rows = (
            await session.execute(
                select(AuditEvaluation.market_id, AuditEvaluation.value_numeric)
                .where(
                    AuditEvaluation.factor_id == self.spec.membership_question,
                    AuditEvaluation.market_id.in_(list(pool_ids)),
                )
                .order_by(AuditEvaluation.id.desc())
            )
        ).all()
        judged: dict[int, float] = {}
        for mid, val in rows:
            if mid not in judged and val is not None:
                judged[mid] = float(val)

        positive = [m for m in markets if judged.get(m.id, -1.0) >= 0.5]
        zero = [m for m in markets if 0.0 <= judged.get(m.id, -1.0) < 0.5]
        log.info(
            "eval.taxonomy.candidates",
            taxonomy=self.spec.taxonomy,
            pool=len(markets),
            positive=len(positive),
            zero=len(zero),
            unjudged=len(markets) - len(positive) - len(zero),
        )
        return CandidateSplit(positive=positive, zero=zero, pool_ids=pool_ids)

    def factor_for(self, question: str) -> FactorSpec:
        by_id = {f.id: f for f in self.ir.factors}
        try:
            return by_id[question]
        except KeyError:
            raise KeyError(
                f"{self.spec.taxonomy} asks for '{question}' but "
                f"{self.spec.index_id} declares no such factor"
            ) from None

    async def predictions(
        self, session: AsyncSession, market_ids: list[int]
    ) -> tuple[dict[int, float], dict]:
        """Selector → Tier 0 cosine gate → stored factor judgment.

        All three together are what decides membership today, and the
        diagnostics separate them so a recall failure can be blamed on the right
        one.
        """
        ir = self.ir
        selected = await select_markets(session, ir)
        selected_ids = {m.id for m in selected}
        survivors, tier0_stats = await _tier0_prefilter(selected, ir)
        surviving_ids = {m.id for m in survivors}

        rows = (
            await session.execute(
                select(AuditEvaluation.market_id, AuditEvaluation.value_numeric)
                .where(
                    AuditEvaluation.factor_id == self.spec.membership_question,
                    AuditEvaluation.market_id.in_(market_ids),
                )
                .order_by(AuditEvaluation.id.desc())
            )
        ).all()
        judged: dict[int, float] = {}
        for mid, val in rows:
            if mid not in judged and val is not None:
                judged[mid] = float(val)

        preds: dict[int, float] = {}
        diag = {
            "selected": 0,
            "not_selected": 0,
            "tier0_dropped": 0,
            "selected_but_unjudged": 0,
            "tier0_enabled": tier0_stats.get("enabled", False),
        }
        for mid in market_ids:
            if mid not in selected_ids:
                preds[mid] = 0.0
                diag["not_selected"] += 1
                continue
            if mid not in surviving_ids:
                preds[mid] = 0.0
                diag["tier0_dropped"] += 1
                continue
            diag["selected"] += 1
            value = judged.get(mid)
            if value is None:
                # Left absent on purpose: "in the pool but never judged" is a
                # real state and must not be laundered into a confident 0.
                diag["selected_but_unjudged"] += 1
                continue
            preds[mid] = value
        return preds, diag


# ──────────────────────────────────────────────────────────────────────────
# The National Economy board (no def, no factors, four regexes)
# ──────────────────────────────────────────────────────────────────────────


class GdpBoardTaxonomy(Taxonomy):
    """Membership as decided by ``engine/gdp_board.py``.

    The chain is: SQL title prefilter → :func:`gdp_board.is_us` →
    :func:`gdp_board.anchor_of` → :func:`gdp_board.parse_reading`. Every one of
    those is a relevance judgment, so the whole chain is the system under test
    and none of it belongs in ``universe_filters``.

    ``parse_reading`` is included in membership even though it looks like a
    parsing detail: a market that survives the first three but yields no
    readable growth number contributes nothing to any anchor, so from the
    index's point of view it is not a member. Its failures are reported
    separately in the diagnostics because the fix for them is different (teach
    the reader a title shape, versus loosen a filter).

    Note the absence of a structural voter for this taxonomy — see
    ``structural.STRUCTURAL_VOTERS``. The obvious deterministic rule here IS the
    system under test, so using it as the protocol's independent vote would be
    pure circularity. GDP's gold therefore rests on model consensus alone, and
    should be read with that caveat.
    """

    def universe_filters(self) -> list[ColumnElement[bool]]:
        """Policy only: open, unresolved, priced, on a venue the board trusts.

        Notably NOT the ``%gdp%`` title prefilter, which is the first link of
        the chain being graded. Excluding it is what lets the ``near_miss``
        stratum surface a GDP-growth market whose title never says "GDP" — a
        false negative the board cannot see and a title-scoped sample could not
        either.

        Play money stays IN, matching the board, which includes manifold and
        metaculus quotes at weight 1.0 rather than dropping them.
        """
        return [
            *live_market_conditions(),
            CoreMarket.last_price.isnot(None),
            CoreMarket.venue.notin_(list(gb.EXCLUDE_VENUES)),
        ]

    def anchors(self) -> list[Anchor]:
        return [Anchor(text=a) for a in self.spec.anchors]

    def _prefilter_clauses(self) -> list[ColumnElement[bool]]:
        """The board's coarse title prefilter, built from its own shared terms."""
        return [
            *(CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REQUIRE_ALL),
            or_(*(CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REQUIRE_ANY)),
            *(~CoreMarket.title.ilike(f"%{t}%") for t in gb.TITLE_REJECT_ANY),
        ]

    @staticmethod
    def _is_member(title: str | None) -> bool:
        return (
            gb.title_prefilter(title)
            and gb.is_us(title)
            and gb.anchor_of(title) is not None
            and gb.parse_reading(title) is not None
        )

    async def candidate_split(self, session: AsyncSession) -> CandidateSplit:
        """Pool = what the prefilter hands the board; split by the rest of the chain.

        The analogue of "the selector returned it": these are the markets the
        board actually looked at. ``zero`` is where the interesting failures
        live — every one of them is a market that says GDP and growth in its
        title and was still thrown away by a regex.
        """
        markets = list(
            (
                await session.execute(
                    select(CoreMarket).where(
                        *self.universe_filters(), *self._prefilter_clauses()
                    )
                )
            )
            .scalars()
            .all()
        )
        pool_ids = {m.id for m in markets}
        positive = [m for m in markets if self._is_member(m.title)]
        keep = {m.id for m in positive}
        zero = [m for m in markets if m.id not in keep]
        log.info(
            "eval.taxonomy.candidates",
            taxonomy=self.spec.taxonomy,
            pool=len(markets),
            positive=len(positive),
            zero=len(zero),
            unjudged=0,
        )
        return CandidateSplit(positive=positive, zero=zero, pool_ids=pool_ids)

    def factor_for(self, question: str) -> FactorSpec:
        """Synthesised, because there is no def to read one from.

        ``weight=None`` is not a placeholder: this factor genuinely does not
        participate in any relevancy aggregation, because the board has no
        relevancy aggregation. It exists only to give the labeller a rubric and
        an answer shape.
        """
        try:
            prompt_ref = self.spec.prompt_refs[question]
        except KeyError:
            raise KeyError(
                f"{self.spec.taxonomy} asks for '{question}' but the spec declares "
                f"no prompt_ref for it (have: {sorted(self.spec.prompt_refs)})"
            ) from None
        return FactorSpec(id=question, type="binary", prompt_ref=prompt_ref, weight=None)

    async def predictions(
        self, session: AsyncSession, market_ids: list[int]
    ) -> tuple[dict[int, float], dict]:
        """Run the chain per market, recording which link rejected it.

        The stage breakdown is the whole reason this is worth measuring. A
        recall failure attributed to ``rejected_other_country`` is a
        word-boundary bug; one attributed to ``rejected_junk`` is an
        over-broad blacklist; one attributed to ``not_prefiltered`` means a real
        GDP market whose title never says GDP. Three different fixes.
        """
        rows = (
            await session.execute(
                select(CoreMarket.id, CoreMarket.title).where(CoreMarket.id.in_(market_ids))
            )
        ).all()
        titles = {mid: title for mid, title in rows}

        preds: dict[int, float] = {}
        diag = {
            "member": 0,
            "not_prefiltered": 0,
            "rejected_junk": 0,
            "rejected_other_country": 0,
            "rejected_not_us": 0,
            "no_anchor": 0,
            "unreadable": 0,
            "missing_market": 0,
        }
        for mid in market_ids:
            title = titles.get(mid)
            if mid not in titles:
                preds[mid] = 0.0
                diag["missing_market"] += 1
                continue
            if not gb.title_prefilter(title):
                preds[mid] = 0.0
                diag["not_prefiltered"] += 1
                continue
            if not gb.is_us(title):
                preds[mid] = 0.0
                # Attribute to the specific regex, mirroring `is_us`'s own order.
                t = title or ""
                if gb._JUNK.search(t):
                    diag["rejected_junk"] += 1
                elif gb._OTHER.search(t):
                    diag["rejected_other_country"] += 1
                else:
                    diag["rejected_not_us"] += 1
                continue
            if gb.anchor_of(title) is None:
                preds[mid] = 0.0
                diag["no_anchor"] += 1
                continue
            if gb.parse_reading(title) is None:
                preds[mid] = 0.0
                diag["unreadable"] += 1
                continue
            preds[mid] = 1.0
            diag["member"] += 1
        return preds, diag


_KINDS: dict[str, type[Taxonomy]] = {
    "index_def": IndexDefTaxonomy,
    "gdp_board": GdpBoardTaxonomy,
}


def resolve_taxonomy(spec: GoldSpec) -> Taxonomy:
    try:
        return _KINDS[spec.kind](spec)
    except KeyError:
        raise KeyError(
            f"unknown taxonomy kind '{spec.kind}'. Known: {sorted(_KINDS)}"
        ) from None


__all__ = [
    "Anchor",
    "CandidateSplit",
    "GdpBoardTaxonomy",
    "IndexDefTaxonomy",
    "Taxonomy",
    "resolve_taxonomy",
]
