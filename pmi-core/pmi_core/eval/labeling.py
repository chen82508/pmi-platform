"""The model-only labelling protocol.

A gold set labelled by one LLM and used to judge another LLM is circular. This
protocol does not remove that circularity — only a human can — but it makes the
circularity *visible and bounded* by requiring three independent things to
agree before a label is admitted as truth:

1. **Two or more distinct labeller models**, none of which should be the model
   currently bound to the factor in production (a labeller that IS the system
   under test grades its own homework).
2. **Self-consistency within each labeller** — ``samples_per_model`` calls at a
   non-zero temperature. A labeller that answers differently on re-ask has no
   stable opinion, and its vote is dropped rather than tie-broken.
3. **A deterministic structural vote** where one exists (see ``structural``).
   This is the only participant that cannot fail in the same direction as the
   models.

Anything short of unanimity is recorded as ``contested`` and never used as
truth. The contested RATE is itself the most useful output: it measures how
underspecified the rubric is, and a rubric that produces 30 % contested is a
finding about the definition, not about any model.

Labels are bound to the rubric that produced them via ``rubric_sha256`` — the
same discipline as ``audit_evaluations.prompt_sha256``. Editing a factor prompt
invalidates the labels made under it.

The prompt template is read straight off disk rather than through
``ensure_prompt``: labelling is a read-only measurement and must not register
prompt rows or touch the SCD2 tables as a side effect.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from pmi_core.config import settings
from pmi_core.dsl.ir import FactorSpec
from pmi_core.engine.pipeline import PROMPTS_DIR
from pmi_core.eval.schema import GoldItem, GoldSpec, Vote
from pmi_core.eval.structural import structural_vote
from pmi_core.eval.taxonomy import Taxonomy, resolve_taxonomy
from pmi_core.llm import get_provider, render_prompt

log = structlog.get_logger(__name__)

STRUCTURAL_SOURCE = "structural"


@dataclass
class LabellerConfig:
    """Who labels, how many times, and how hard consensus has to be.

    ``temperature`` is deliberately non-zero: at 0 the repeated samples are
    near-identical and self-consistency degenerates into an expensive way of
    asking once. The point is to detect the markets where the model has no
    stable view.
    """

    models: tuple[str, ...] = ("gpt-4o-mini-2024-07-18",)
    samples_per_model: int = 3
    temperature: float = 0.3
    concurrency: int = 0  # 0 → settings.llm_concurrency

    def effective_concurrency(self) -> int:
        return self.concurrency or settings.llm_concurrency


@dataclass
class LabelStats:
    calls: int = 0
    failures: int = 0
    cost_usd: float = 0.0
    labelled: int = 0
    contested: int = 0
    unstable_labellers: int = 0
    structural_conflicts: int = 0
    per_question_contested: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "cost_usd": round(self.cost_usd, 4),
            "labelled": self.labelled,
            "contested": self.contested,
            "unstable_labeller_votes": self.unstable_labellers,
            "structural_conflicts": self.structural_conflicts,
            "contested_by_question": dict(self.per_question_contested),
        }


def load_rubric(prompt_ref: str) -> tuple[str, str]:
    """Return ``(template, sha256)`` for a factor's prompt_ref, read from disk."""
    if not prompt_ref.startswith("prompts/"):
        raise ValueError(f"prompt_ref must start with 'prompts/': {prompt_ref}")
    path: Path = PROMPTS_DIR / f"{prompt_ref.removeprefix('prompts/')}.md"
    template = path.read_text(encoding="utf-8")
    return template, hashlib.sha256(template.encode("utf-8")).hexdigest()


def _factors_for(
    spec: GoldSpec, tax: Taxonomy, questions: Sequence[str] | None = None
) -> dict[str, FactorSpec]:
    """Resolve which factors to label, defaulting to the whole spec.

    A subset is worth supporting because each question costs its own LLM call —
    the "one call answers several factors" economy described for the production
    batch path is NOT what this module does. Labelling only the membership
    question is therefore a 3× saving on a taxonomy with three, which matters
    when the local labeller runs ~8 s/call on a GPU that production scoring is
    also using.
    """
    wanted = tuple(questions) if questions else spec.questions
    unknown = [q for q in wanted if q not in spec.questions]
    if unknown:
        raise KeyError(f"{unknown} not in {spec.taxonomy} questions {list(spec.questions)}")
    return {q: tax.factor_for(q) for q in wanted}


def _stable(values: list[float]) -> float | None:
    """A labeller's answer, or None if its own samples disagreed."""
    if not values:
        return None
    first = values[0]
    return first if all(v == first for v in values) else None


def _consensus(votes: list[Vote]) -> tuple[float | None, bool]:
    """Fold votes into ``(value, unanimous)``.

    Abstentions (``value is None``) do not block consensus — a structural rule
    with no opinion should not make an item contested. A single lone voter is
    NOT enough: with nothing to corroborate it there is no protocol left, just
    one model's opinion, so it is reported contested.
    """
    opinions = [v.value for v in votes if v.value is not None]
    if len(opinions) < 2:
        return None, False
    first = opinions[0]
    if all(o == first for o in opinions):
        return first, True
    return None, False


async def _one_call(
    sem: asyncio.Semaphore,
    item: GoldItem,
    factor: FactorSpec,
    template: str,
    model_id: str,
    temperature: float,
) -> tuple[float | None, float, str | None, float]:
    """One labeller call. Returns (value, confidence, rationale, cost_usd).

    Failures return ``(None, ...)`` rather than raising: a labeller that errors
    on one market has simply not voted, which the consensus rule already knows
    how to handle. Substituting a stub here — as the production path does — would
    silently inject a fabricated judgment into the reference set.
    """
    async with sem:
        provider = get_provider(model_id)
        try:
            resp = await provider.evaluate(
                rendered_prompt=render_prompt(template, item),
                factor=factor,
                temperature=temperature,
                market=item,
            )
        except Exception as exc:  # noqa: BLE001 - a failed vote is an absent vote
            log.warning(
                "eval.labeling.call_failed",
                market_id=item.market_id,
                factor=factor.id,
                model=model_id,
                error=str(exc)[:160],
            )
            return None, 0.0, None, 0.0
        return (
            resp.value_numeric,
            resp.confidence or 0.0,
            resp.rationale,
            float(getattr(resp, "cost_usd", 0.0) or 0.0),
        )


async def label_items(
    spec: GoldSpec,
    items: list[GoldItem],
    config: LabellerConfig | None = None,
    *,
    relabel: bool = False,
    questions: Sequence[str] | None = None,
) -> tuple[list[GoldItem], LabelStats]:
    """Label (or re-label) gold items in place. Returns the items + run stats.

    Already-labelled items are skipped unless ``relabel`` — labelling is the
    only part of this harness that costs money, and a re-run after adding a
    stratum should only pay for the new rows.
    """
    cfg = config or LabellerConfig()
    factors = _factors_for(spec, resolve_taxonomy(spec), questions)
    rubrics = {q: load_rubric(f.prompt_ref) for q, f in factors.items()}

    todo = [it for it in items if relabel or it.status == "unlabelled"]
    stats = LabelStats()
    if not todo:
        log.info("eval.labeling.nothing_to_do", taxonomy=spec.taxonomy)
        return items, stats

    sem = asyncio.Semaphore(cfg.effective_concurrency())

    # One flat fan-out over (item, question, model, sample) — the unit of work
    # is a single call, so a slow labeller can't serialise the whole run behind
    # its own repeats.
    plan: list[tuple[int, str, str]] = []
    coros = []
    for it in todo:
        for question, factor in factors.items():
            template, _sha256 = rubrics[question]
            for model_id in cfg.models:
                for _ in range(cfg.samples_per_model):
                    plan.append((it.market_id, question, model_id))
                    coros.append(
                        _one_call(sem, it, factor, template, model_id, cfg.temperature)
                    )

    log.info(
        "eval.labeling.start",
        taxonomy=spec.taxonomy,
        items=len(todo),
        questions=len(factors),
        labellers=list(cfg.models),
        calls=len(coros),
    )
    results = await asyncio.gather(*coros)

    # (market_id, question, model) → list of sampled values
    samples: dict[tuple[int, str, str], list[float]] = {}
    rationales: dict[tuple[int, str, str], tuple[float, str | None]] = {}
    for (mid, question, model_id), (value, conf, rationale, cost) in zip(
        plan, results, strict=True
    ):
        stats.calls += 1
        stats.cost_usd += cost
        if value is None:
            stats.failures += 1
            continue
        samples.setdefault((mid, question, model_id), []).append(float(value))
        rationales.setdefault((mid, question, model_id), (conf, rationale))

    for it in todo:
        # Clear only the questions being (re-)labelled. A subset run must not
        # discard labels another run already paid for, and a stale label left
        # behind for a question in `factors` would be worse than none — it would
        # be attributed to this run's rubric sha.
        for question in factors:
            it.votes.pop(question, None)
            it.labels.pop(question, None)
        it.rubric_sha256.update({q: sha for q, (_t, sha) in rubrics.items()})
        for question in factors:
            votes: list[Vote] = []
            for model_id in cfg.models:
                vals = samples.get((it.market_id, question, model_id), [])
                stable = _stable(vals)
                if stable is None and vals:
                    stats.unstable_labellers += 1
                conf, rationale = rationales.get((it.market_id, question, model_id), (0.0, None))
                votes.append(
                    Vote(source=model_id, value=stable, confidence=conf, rationale=rationale)
                )
            sv = structural_vote(question, it.title, it.description)
            if sv is not None:
                votes.append(Vote(source=STRUCTURAL_SOURCE, value=sv))

            value, unanimous = _consensus(votes)
            it.votes[question] = votes
            if unanimous and value is not None:
                it.labels[question] = value
            else:
                stats.per_question_contested[question] += 1
                if sv is not None and any(
                    v.value is not None and v.value != sv and v.source != STRUCTURAL_SOURCE
                    for v in votes
                ):
                    stats.structural_conflicts += 1

        it.labelled_at = datetime.now(UTC)
        if spec.membership_question in it.labels:
            it.status = "labelled"
            stats.labelled += 1
        elif spec.membership_question in factors:
            # Asked, and the labellers split. That is a contested item.
            it.status = "contested"
            stats.contested += 1
        # Otherwise this run never asked the membership question (a subset run
        # for some other factor), so the item's status is not this run's to
        # decide — leaving it 'unlabelled' keeps `eval score` honest about
        # having no truth for it yet.

    log.info("eval.labeling.done", taxonomy=spec.taxonomy, **stats.as_dict())
    return items, stats


def readjudicate(
    spec: GoldSpec, items: list[GoldItem], exclude: Collection[str]
) -> tuple[list[GoldItem], dict]:
    """Recompute labels from the STORED votes with some labellers removed.

    This is why ``Vote`` is persisted per source instead of being folded into a
    consensus at labelling time. A gold set is only usable as a yardstick for a
    system that did not help build it, and on the first live run both systems
    under test had: ``ollama/gemma3:4b`` is the production BTC factor *and* was a
    labeller, and the structural rule is both a candidate classifier *and* the
    protocol's independent vote. Scoring either against that gold measures
    agreement with itself.

    Dropping a source and re-deriving consensus fixes it for free — no LLM calls,
    no re-sampling. The cost is coverage: with one labeller removed, questions
    where the remaining voter abstains fall below the two-opinion minimum and
    become contested, so the honest number is computed over fewer items. That
    trade is the right one; a wide, self-confirming sample is worth less than a
    narrow independent one.

    Returns copies — the caller's items (and the file on disk) are untouched.
    """
    excluded = set(exclude)
    out: list[GoldItem] = []
    stats = {"labelled": 0, "contested": 0, "excluded_sources": sorted(excluded)}
    for it in items:
        clone = it.model_copy(deep=True)
        clone.labels = {}
        for question, votes in it.votes.items():
            value, unanimous = _consensus([v for v in votes if v.source not in excluded])
            if unanimous and value is not None:
                clone.labels[question] = value
        if spec.membership_question in clone.labels:
            clone.status = "labelled"
            stats["labelled"] += 1
        else:
            clone.status = "contested"
            stats["contested"] += 1
        out.append(clone)
    return out, stats


__all__ = [
    "LabelStats",
    "LabellerConfig",
    "label_items",
    "load_rubric",
    "readjudicate",
]
