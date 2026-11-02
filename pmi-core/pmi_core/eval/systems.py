"""Systems under test — things that produce a membership prediction per market.

Each returns ``{market_id: 0..1}``. A market deliberately LEFT OUT of the
mapping means "this system never even considered it", which ``metrics`` scores
as a miss and counts separately. That distinction matters: a selector that
returns nothing would otherwise post perfect precision.

``live`` is whatever the taxonomy says the running system is — a real
selector plus Tier 0 plus a cached LLM judgment for an index def, four regexes
for the National Economy board. Both come back with a diagnostic breakdown
attributing each non-membership to the stage that caused it, which is the only
way a recall number turns into a thing you can go and fix.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from pmi_core.db import session_scope
from pmi_core.eval.schema import GoldItem, GoldSpec
from pmi_core.eval.structural import structural_vote
from pmi_core.eval.taxonomy import resolve_taxonomy

log = structlog.get_logger(__name__)


async def live_predictions(
    spec: GoldSpec, market_ids: Iterable[int]
) -> tuple[dict[int, float], dict]:
    """What the running system says about each gold market today."""
    ids = list(market_ids)
    tax = resolve_taxonomy(spec)
    async with session_scope() as session:
        preds, diag = await tax.predictions(session, ids)
    log.info("eval.systems.live", taxonomy=spec.taxonomy, kind=spec.kind, **diag)
    return preds, diag


def structural_predictions(spec: GoldSpec, items: list[GoldItem]) -> dict[int, float]:
    """The $0 reference: title/venue structure only, no model, no embedding.

    Worth running first on every taxonomy. If a deterministic rule lands within
    a point or two of the live LLM path, that is the finding — it means the
    LLM's contribution to membership is nearly all reproducible for free, and
    the cascade's cheap layer can carry it.

    Abstentions are emitted as 0.0 rather than omitted: a rule with no opinion
    is claiming non-membership here, which is the conservative reading and
    keeps the comparison honest (it takes the recall penalty for abstaining).

    Meaningless for a taxonomy whose live system IS a deterministic rule — on
    the GDP board this would score the regex chain against itself. ``has_structural``
    reports whether there is anything to run.
    """
    out: dict[int, float] = {}
    for it in items:
        vote = structural_vote(spec.membership_question, it.title, it.description)
        out[it.market_id] = 0.0 if vote is None else float(vote)
    return out


def labeller_predictions(
    spec: GoldSpec, items: list[GoldItem], source: str
) -> dict[int, float]:
    """One labeller's own votes, replayed as if it were the production model.

    This is how you choose what to bind a factor to without paying for the
    experiment twice. Labelling already asked every candidate model the
    membership question on every gold item and stored the answers per source, so
    "how would this model score if we promoted it" is a lookup, not a run.

    Only meaningful against gold that excludes this source — otherwise the model
    is being graded against a consensus it helped write, which is the failure
    ``--exclude-labeller`` exists to correct. The caller is responsible for
    passing re-adjudicated items; `cli.eval_score` does.

    Abstentions become 0.0 for the same reason as ``structural_predictions``: a
    model that returns nothing is claiming non-membership, and should take the
    recall penalty for it rather than have the item quietly dropped.
    """
    out: dict[int, float] = {}
    for it in items:
        votes = [
            v
            for v in it.votes.get(spec.membership_question, [])
            if v.source == source and v.value is not None
        ]
        if not votes:
            continue
        out[it.market_id] = sum(float(v.value) for v in votes) / len(votes)
    return out


def labeller_sources(spec: GoldSpec, items: list[GoldItem]) -> list[str]:
    """Every source that voted on the membership question, models and rules."""
    seen = {
        v.source for it in items for v in it.votes.get(spec.membership_question, [])
    }
    return sorted(seen)


__all__ = [
    "labeller_predictions",
    "labeller_sources",
    "live_predictions",
    "structural_predictions",
]
