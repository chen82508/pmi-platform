"""Partial-tick guard: refuse to publish a score whose membership collapsed.

Why this exists (2026-10-06): the BTC Bull/Bear headline jumped 45 → 57 → 45
inside an hour while the market did nothing. The 57 came from a tick that
aggregated 28 markets instead of the usual ~180: the selector's `max_markets`
cap was binding and the semantic rank that orders it is an approximate ANN
walk over an embedding table dominated by dead ladders, so on a bad walk the
cap filled with the newest unembedded strikes and the real members fell out.
The pipeline run still reported `succeeded`, the score row was written, and
every reader (page cache, hub strip, Ask Micah) served it as the latest level.

A collapsed member set is not a market observation, so it must not become the
headline. The verdict here is deliberately dumb and local: compare this tick's
``component_count`` with the trailing median of the index's own recent
published ticks and hold the tick when it is far below. Holding forever would
freeze an index whose membership genuinely shrank (markets resolve, a venue
goes quiet), so a hold expires after ``max_hold_hours`` without any published
score: the next tick then publishes — flagged — and the trailing median adapts.

Pure function + frozen dataclass so the policy is unit-testable without a DB;
``pipeline.run_pipeline`` supplies the counts and acts on ``publish``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import median


@dataclass(frozen=True)
class TickGuardVerdict:
    partial: bool
    publish: bool
    reason: str
    component_count: int
    baseline_median: float | None
    baseline_n: int
    min_ratio: float
    latest_published_age_h: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def partial_tick_verdict(
    component_count: int,
    baseline_counts: list[int],
    *,
    latest_published_at: datetime | None,
    now: datetime,
    min_ratio: float,
    min_baseline: int,
    max_hold_hours: float,
) -> TickGuardVerdict:
    """Decide whether a tick with ``component_count`` members may be published.

    ``baseline_counts``: component counts of this index definition's recently
    published ticks (any order). ``latest_published_at``: ``as_of`` of the most
    recent published tick, used to expire a hold.

    Reasons:
      * ``no_baseline``        – nothing to compare against (fresh version) → publish
      * ``baseline_too_small`` – tiny index, ratio is noise → publish
      * ``ok``                 – within ratio → publish
      * ``held``               – collapsed, a recent score exists → do NOT publish
      * ``hold_expired``       – collapsed, but nothing published for too long → publish, flagged
    """
    age_h: float | None = None
    if latest_published_at is not None:
        age_h = max(0.0, (now - latest_published_at).total_seconds() / 3600.0)

    def verdict(partial: bool, publish: bool, reason: str, base: float | None) -> TickGuardVerdict:
        return TickGuardVerdict(
            partial=partial,
            publish=publish,
            reason=reason,
            component_count=component_count,
            baseline_median=base,
            baseline_n=len(baseline_counts),
            min_ratio=min_ratio,
            latest_published_age_h=age_h,
        )

    if not baseline_counts:
        return verdict(False, True, "no_baseline", None)
    base = float(median(baseline_counts))
    if base < min_baseline:
        return verdict(False, True, "baseline_too_small", base)
    if component_count >= min_ratio * base:
        return verdict(False, True, "ok", base)
    # Collapsed. Hold unless the index has gone unpublished for too long.
    if age_h is None or age_h >= max_hold_hours:
        return verdict(True, True, "hold_expired", base)
    return verdict(True, False, "held", base)
