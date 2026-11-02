"""Detect & collapse mutually-exclusive bucket markets.

Polymarket (and other prediction venues) frequently split a single question
into daily / weekly / multi-year deadline buckets:

    "Will the US strike Iran on June 20, 2026?"
    "Will the US strike Iran on June 21, 2026?"
    "Will Ethiopia bring a 5th-gen fighter into service by the end of 2030?"
    "Will Ethiopia bring a 5th-gen fighter into service by the end of 2035?"

These are mutually exclusive — only one outcome can occur. Treating each
as an independent component double-counts the underlying event and skews
the aggregated PMI. This module detects such groups and replaces all
members with a single synthetic row whose price is the *combined*
probability:

  * **Calendar buckets** (≥ 2 parseable dates within `max_spread_days`):
    noisy-OR aggregation, ``p = 1 - Π (1 - p_i)`` — correct under the
    independence approximation.
  * **Multi-year / undated deadline variants** (no two parseable dates
    within the window — e.g. "by end of 2030" vs "by end of 2040"):
    arithmetic mean — neither sum-capped nor noisy-OR are appropriate
    because the deadlines aren't independent events.

Ported from Micah's `micah-job-executor/.../bucket_collapser.py`
(2026-05-29 — PR #15 "noisy-OR + mean"). Adapted for the platform's
`MarketEvaluations` shape (no `source` dimension — Polymarket-only —
and the per-index `max_spread_days` lives in `CollapseSpec`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

from pmi_core.dsl.ir import IndexDef
from pmi_core.utils.date_analyzer import (
    is_active_bucket,
    parse_bucket_date,
    strip_date_suffix,
)
from pmi_core.utils.dates import utc_today


@dataclass
class BucketGroup:
    """A detected group of mutually-exclusive bucket markets."""

    base_question: str
    representative_idx: int
    member_indices: list[int]
    collapsed_probability: float
    mode: str  # "noisy_or" | "mean" | "passthrough"


def _clamp_probability(p: float) -> float:
    return max(0.0, min(1.0, float(p)))


def _aggregate_subgroup_probability(
    sub_group: list[int],
    rows: list,
    *,
    max_spread_days: int,
) -> tuple[float, str]:
    """Combine bucket member probabilities for scoring.

    Returns ``(probability, mode)`` where ``mode`` is one of:

    * ``"noisy_or"`` — 2+ parseable dates within ``max_spread_days``;
      ``1 - Π (1 - p_i)``.
    * ``"mean"``     — otherwise; arithmetic mean of clamped probs.
    * ``"passthrough"`` — singleton or empty sub-group.
    """
    probs = [_clamp_probability(_market_price(rows[i])) for i in sub_group]
    if len(probs) <= 1:
        return (probs[0] if probs else 0.0, "passthrough")

    dated: list[date] = []
    for i in sub_group:
        bd = parse_bucket_date(_market_title(rows[i]))
        if bd is not None:
            dated.append(bd)
    if len(dated) >= 2 and (max(dated) - min(dated)).days <= max_spread_days:
        complement = 1.0
        for p in probs:
            complement *= 1.0 - p
        return (min(1.0, 1.0 - complement), "noisy_or")

    return (sum(probs) / len(probs), "mean")


def _market_title(row) -> str:  # noqa: ANN001 — accepts MarketEvaluations duck-typed
    return row.market.title


def _market_price(row) -> float:  # noqa: ANN001
    """Polymarket: last price ≈ implied probability. None → 0.0."""
    return float(row.last_price) if row.last_price is not None else 0.0


def _split_by_date_gap(
    active: list[int],
    rows: list,
    *,
    max_spread_days: int,
) -> list[list[int]]:
    """Split active members into consecutive sub-groups by date proximity.

    Sorts members by parsed date, walks consecutive pairs. When the gap
    exceeds ``max_spread_days``, a new sub-group starts. Members whose
    dates cannot be parsed join the largest sub-group (so they still get
    collapsed under multi-year / undated cases via the "mean" branch).
    """
    dated: list[tuple[date, int]] = []
    undated: list[int] = []
    for i in active:
        d = parse_bucket_date(_market_title(rows[i]))
        if d is not None:
            dated.append((d, i))
        else:
            undated.append(i)

    if len(dated) < 2:
        # Not enough parseable dates to split — keep as one group.
        # (Undated members still get aggregated via _aggregate_subgroup_probability.)
        return [active]

    dated.sort(key=lambda x: x[0])

    sub_groups: list[list[int]] = [[dated[0][1]]]
    for j in range(1, len(dated)):
        gap = (dated[j][0] - dated[j - 1][0]).days
        if gap > max_spread_days:
            sub_groups.append([dated[j][1]])
            continue
        sub_groups[-1].append(dated[j][1])

    if undated:
        largest = max(sub_groups, key=len)
        largest.extend(undated)

    return sub_groups


def _pick_representative(sub_group: list[int], rows: list, mode: str) -> int:
    """Pick the representative index for a sub-group.

    Currently honors ``"max_probability"`` (highest last_price). Other
    modes (``"highest_liquidity"``, ``"newest"``) fall back to that
    because the platform doesn't yet thread liquidity / ts into the
    collapser. CORR-3.4 / CORR-3.5 land those signals.
    """
    if mode == "newest":
        # Closes_at as proxy for "newest" (last to settle = freshest signal)
        return max(
            sub_group,
            key=lambda i: (rows[i].market.closes_at or rows[i].market.created_at),
        )
    return max(sub_group, key=lambda i: _market_price(rows[i]))


def _make_bucket_group(
    sub_group: list[int],
    rows: list,
    *,
    max_spread_days: int,
    rep_mode: str,
) -> BucketGroup:
    best_idx = _pick_representative(sub_group, rows, rep_mode)
    collapsed_prob, mode = _aggregate_subgroup_probability(
        sub_group, rows, max_spread_days=max_spread_days
    )
    rep_base = strip_date_suffix(_market_title(rows[best_idx]))
    return BucketGroup(
        base_question=rep_base,
        representative_idx=best_idx,
        member_indices=sub_group,
        collapsed_probability=collapsed_prob,
        mode=mode,
    )


# A range / distribution bin ("between $A and $B", "$A to $B", "60,000 to
# 64,999.99"). Its per-date variants ("...on Jul 15" / "...on Jul 17") are
# NOT mutually-exclusive calendar siblings — they are independent per-day price
# distributions — so they must not be noisy-OR'd across dates like a "strike X
# on June 20 / June 21" event ladder would be.
_RANGE_BIN_RE = re.compile(
    r"\bbetween\b|\d[\d,]{2,}\s*(?:to|-|\u2013)\s*\d[\d,]{2,}",
    re.IGNORECASE,
)


def _is_range_bin(title: str) -> bool:
    return bool(_RANGE_BIN_RE.search(title or ""))


def detect_bucket_groups(
    rows: list,
    ir: IndexDef,
    *,
    ref_date: date | None = None,
) -> list[BucketGroup]:
    """Detect groups of mutually-exclusive bucket markets among ``rows``.

    Group key = ``base_question`` (title with date suffix stripped, lower-cased).
    Only groups with 2+ active members qualify. Within each candidate group,
    members are split into sub-groups using ``ir.aggregation.collapse.max_spread_days``
    so that e.g. "June 20" + "August 1" become two separate sub-groups.
    """
    if not ir.aggregation.collapse.enabled:
        return []

    max_spread_days = ir.aggregation.collapse.max_spread_days
    rep_mode = ir.aggregation.collapse.representative

    groups: dict[str, list[int]] = {}
    for idx, r in enumerate(rows):
        title = _market_title(r)
        base = strip_date_suffix(title)
        if base == title:
            continue  # no date suffix → not a bucket market
        if _is_range_bin(title):
            continue  # per-date price-distribution bin — dates are independent
        groups.setdefault(base.lower(), []).append(idx)

    as_of = ref_date or utc_today()
    result: list[BucketGroup] = []
    for indices in groups.values():
        active = [
            i for i in indices if is_active_bucket(_market_title(rows[i]), as_of)
        ]
        if len(active) < 2:
            continue
        for sub_group in _split_by_date_gap(
            active, rows, max_spread_days=max_spread_days
        ):
            if len(sub_group) >= 2:
                result.append(
                    _make_bucket_group(
                        sub_group,
                        rows,
                        max_spread_days=max_spread_days,
                        rep_mode=rep_mode,
                    )
                )
    return result


def collapse_for_scoring(
    rows: list,
    ir: IndexDef,
    *,
    ref_date: date | None = None,
) -> list:
    """Collapse detected bucket groups; return rows ready for aggregation.

    For each group: emit one synthetic row built from the representative
    with ``last_price`` replaced by the collapsed probability. Non-bucket
    rows pass through unchanged. The synthetic's ``by_factor`` mirrors
    the representative — so lineage IDs in the resulting score row point
    at the rep's evaluations only (siblings remain in ``audit_evaluations``
    but are not linked from ``ts_index_scores.component_evaluation_ids``).
    """
    groups = detect_bucket_groups(rows, ir, ref_date=ref_date)
    if not groups:
        return list(rows)

    consumed: set[int] = set()
    synthetic_map: dict[int, object] = {}

    for g in groups:
        consumed.update(g.member_indices)
        rep = rows[g.representative_idx]
        # ``replace`` works on dataclasses; MarketEvaluations is one.
        synthetic_map[g.representative_idx] = replace(
            rep, last_price=g.collapsed_probability
        )

    return [
        synthetic_map[i] if i in synthetic_map else r
        for i, r in enumerate(rows)
        if i in synthetic_map or i not in consumed
    ]
