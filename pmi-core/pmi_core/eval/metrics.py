"""Scoring a classifier against a gold set.

Three numbers, deliberately reported side by side rather than collapsed into
one, because they answer different questions and can disagree:

**sample** — plain counts over the labelled items. Easy to reason about, and
*not* an estimate of anything: the sample is stratified on purpose, so positives
are massively over-represented relative to the corpus.

**population** — each item re-weighted by ``population / sampled`` for its
stratum (Horvitz-Thompson). This is the honest precision/recall. The gap
between it and ``sample`` is usually large and always instructive: precision is
dominated by how much junk the rare strata contribute once scaled back up.

**impact** — population weights multiplied by the market's liquidity weight.
A false positive on an untraded Kalshi strike and a false positive on a deep
Polymarket book are one error each in the first two views, but the second one
is the only one that can move a score. The ladder is imported from the
aggregator rather than re-declared so it cannot drift from the weighting the
index actually applies.

What this module deliberately does NOT do: claim to predict the index delta.
Membership error and score error are related but not the same function, and the
exact answer already exists — replay the period through
``engine.backfill_scores`` under both systems and diff the series. ``impact`` is
the fast proxy you gate CI on; the replay is the thing you trust before
promoting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pmi_core.engine.aggregator import (
    _LIQUIDITY_BUCKETS,
    _LIQUIDITY_MIN_SAMPLE,
    _percentile,
)
from pmi_core.eval.schema import GoldItem, GoldSetMeta, GoldSpec


@dataclass(frozen=True)
class Confusion:
    """Weighted confusion counts. Floats, not ints — entries carry weights."""

    tp: float = 0.0
    fp: float = 0.0
    fn: float = 0.0
    tn: float = 0.0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom > 0 else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom > 0 else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def accuracy(self) -> float | None:
        total = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / total if total > 0 else None

    @property
    def positive_rate(self) -> float | None:
        """Share of the (weighted) universe the classifier calls a member."""
        total = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.fp) / total if total > 0 else None

    def as_dict(self) -> dict:
        return {
            "tp": round(self.tp, 4),
            "fp": round(self.fp, 4),
            "fn": round(self.fn, 4),
            "tn": round(self.tn, 4),
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
            "accuracy": _round(self.accuracy),
            "positive_rate": _round(self.positive_rate),
        }


def _round(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


@dataclass
class EvalReport:
    taxonomy: str
    system: str
    n_gold: int
    n_scored: int
    n_contested: int
    n_missing_prediction: int
    sample: Confusion
    population: Confusion
    impact: Confusion
    by_stratum: dict[str, Confusion] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "taxonomy": self.taxonomy,
            "system": self.system,
            "n_gold": self.n_gold,
            "n_scored": self.n_scored,
            "n_contested_excluded": self.n_contested,
            "n_missing_prediction": self.n_missing_prediction,
            "sample": self.sample.as_dict(),
            "population": self.population.as_dict(),
            "impact": self.impact.as_dict(),
            "by_stratum": {k: v.as_dict() for k, v in sorted(self.by_stratum.items())},
            "errors": self.errors,
        }


def liquidity_weights(items: list[GoldItem]) -> dict[int, float]:
    """Quantile ladder over the gold sample, mirroring the aggregator.

    Reuses the aggregator's own bucket ladder and percentile helper so an
    "impact" here means the same thing it means at scoring time. Markets with
    no liquidity signal weigh 1.0 (never 0) for the same reason the aggregator
    does it: a cold-start market with no depth row yet is not a market that
    cannot matter.

    Play-money markets are excluded from the quantile SAMPLE — their volume is
    site credits, so letting it define the percentiles corrupts the ladder —
    while still carrying a weight, again matching ``_liquidity_weights``.
    """
    weights = {it.market_id: 1.0 for it in items}
    samples = sorted(
        float(it.liquidity)
        for it in items
        if it.liquidity is not None and it.liquidity > 0 and not it.is_play_money
    )
    if len(samples) < _LIQUIDITY_MIN_SAMPLE or samples[0] == samples[-1]:
        return weights

    p20 = _percentile(samples, 0.20)
    p50 = _percentile(samples, 0.50)
    p80 = _percentile(samples, 0.80)
    for it in items:
        if it.liquidity is None or it.liquidity <= 0 or it.is_play_money:
            continue
        d = float(it.liquidity)
        if d < p20:
            weights[it.market_id] = _LIQUIDITY_BUCKETS[0][1]
        elif d < p50:
            weights[it.market_id] = _LIQUIDITY_BUCKETS[1][1]
        elif d < p80:
            weights[it.market_id] = _LIQUIDITY_BUCKETS[2][1]
        else:
            weights[it.market_id] = _LIQUIDITY_BUCKETS[3][1]
    return weights


def _add(c: Confusion, truth: bool, pred: bool, w: float) -> Confusion:
    if truth and pred:
        return Confusion(c.tp + w, c.fp, c.fn, c.tn)
    if not truth and pred:
        return Confusion(c.tp, c.fp + w, c.fn, c.tn)
    if truth and not pred:
        return Confusion(c.tp, c.fp, c.fn + w, c.tn)
    return Confusion(c.tp, c.fp, c.fn, c.tn + w)


def evaluate_membership(
    spec: GoldSpec,
    meta: GoldSetMeta,
    items: list[GoldItem],
    predictions: dict[int, float],
    *,
    system: str,
    threshold: float = 0.5,
    max_errors: int = 40,
) -> EvalReport:
    """Score ``predictions`` (market_id → 0..1) against the gold membership label.

    Contested items are excluded from every confusion count and reported
    separately. They are not failures of the classifier — they are places where
    the labellers could not agree, which usually means the RUBRIC is
    underspecified. Folding them in either way would launder a definition gap
    into a model score.

    A market present in the gold set but absent from ``predictions`` is counted
    as a negative prediction, not skipped: "the pipeline never looked at it" is
    exactly what a false negative is, and silently dropping those is how a
    selector that returns nothing scores perfect precision.
    """
    weights = meta.weights()
    liq = liquidity_weights(items)

    sample = population = impact = Confusion()
    by_stratum: dict[str, Confusion] = {}
    errors: list[dict] = []
    n_contested = 0
    n_missing = 0
    n_scored = 0

    for it in items:
        if it.status != "labelled":
            n_contested += 1
            continue
        truth_val = it.membership(spec)
        if truth_val is None:
            n_contested += 1
            continue

        raw = predictions.get(it.market_id)
        if raw is None:
            n_missing += 1
            raw = 0.0
        truth = truth_val >= threshold
        pred = raw >= threshold
        n_scored += 1

        w_pop = weights.get(it.stratum, 1.0)
        w_imp = w_pop * liq.get(it.market_id, 1.0)

        sample = _add(sample, truth, pred, 1.0)
        population = _add(population, truth, pred, w_pop)
        impact = _add(impact, truth, pred, w_imp)
        by_stratum[it.stratum] = _add(
            by_stratum.get(it.stratum, Confusion()), truth, pred, 1.0
        )

        if truth != pred and len(errors) < max_errors:
            errors.append(
                {
                    "market_id": it.market_id,
                    "venue": it.venue,
                    "title": it.title[:110],
                    "stratum": it.stratum,
                    "gold": truth_val,
                    "predicted": round(raw, 4),
                    "kind": "false_negative" if truth else "false_positive",
                    "liquidity_weight": round(liq.get(it.market_id, 1.0), 3),
                }
            )

    return EvalReport(
        taxonomy=spec.taxonomy,
        system=system,
        n_gold=len(items),
        n_scored=n_scored,
        n_contested=n_contested,
        n_missing_prediction=n_missing,
        sample=sample,
        population=population,
        impact=impact,
        by_stratum=by_stratum,
        errors=errors,
    )


def compare(baseline: EvalReport, candidate: EvalReport) -> dict:
    """Deltas between two systems on the same gold set.

    ``regression`` is judged on the impact view: that is the one that tracks
    whether the index moves, and a change that trades a few untraded-strike
    false positives for one deep-book false negative should read as a
    regression even when the plain counts improve.
    """

    def _delta(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(b - a, 4)

    out: dict = {"baseline": baseline.system, "candidate": candidate.system, "views": {}}
    worse = []
    for view in ("sample", "population", "impact"):
        b: Confusion = getattr(baseline, view)
        c: Confusion = getattr(candidate, view)
        out["views"][view] = {
            "precision": [_round(b.precision), _round(c.precision), _delta(b.precision, c.precision)],
            "recall": [_round(b.recall), _round(c.recall), _delta(b.recall, c.recall)],
            "f1": [_round(b.f1), _round(c.f1), _delta(b.f1, c.f1)],
        }
        if view == "impact":
            for metric in ("precision", "recall"):
                d = _delta(getattr(b, metric), getattr(c, metric))
                if d is not None and d < 0:
                    worse.append(f"impact {metric} {d:+.4f}")
    out["regression"] = worse or None
    return out


__all__ = [
    "Confusion",
    "EvalReport",
    "compare",
    "evaluate_membership",
    "liquidity_weights",
]
