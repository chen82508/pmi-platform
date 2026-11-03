"""Aggregator: collapse mutually-exclusive bucket → weighted aggregate of relevancy × direction.

Bucket collapse uses date-aware grouping with noisy-OR for calendar buckets
and arithmetic mean for multi-year deadline variants — see
``pmi_core.engine.bucket_collapser``. Ported from Micah's
``bucket_collapser.py`` (2026-05-29) which replaced the older
``mutually_exclusive.py`` sum-capped-at-1 approach.

CORR-3.4 — quantile liquidity weighting using ``ts_orderbook_snapshots.
*_depth_1pct`` as the primary signal and ``ts_price_snapshots.volume_24h``
as the cold-start fallback. Ladder mirrors Micah's
``source_weights.get_source_weight`` (0.90 / 1.00 / 1.20 / 1.50) so the
shape is familiar; only the *input* changed from cross-source volume to
intra-venue book depth, which is a better proxy for "is this price
trustworthy".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pmi_core.dsl.ir import ExtractionSpec, IndexDef
from pmi_core.engine.bucket_collapser import collapse_for_scoring
from pmi_core.engine.election_cycle import election_cycle
from pmi_core.engine.extraction import (
    _GRAMMARS,
    allowed_states_for,
    classify_entities,
    extraction_race_type,
    parse_label_for,
    rollup_parent,
)
from pmi_core.engine.geo import (
    REGION_NAME,
    countries_in_region,
    country_name,
    gdp_pop_weights,
    region_of,
    region_weights,
)
from pmi_core.engine.incumbency import incumbent_party, is_open_seat
from pmi_core.engine.liquidity import has_liquidity
from pmi_core.engine.moneyness import moneyness_weights
from pmi_core.engine.race_identity import exclusion_reason
from pmi_core.engine.seat_distribution import compute_seat_distribution
from pmi_core.engine.seat_mapping import (
    EntityObservation,
    RaceReduction,
    _venue_rank,
    candidate_label_from_title,
    reduce_races,
    resolve_candidate_party,
    state_code,
)
from pmi_core.models import AuditEvaluation, CoreMarket, CoreMarketRaceTag
from pmi_core.venues import has_real_money, is_play_money_venue


@dataclass
class MarketEvaluations:
    market: CoreMarket
    by_factor: dict[str, AuditEvaluation]
    last_price: float | None
    # CORR-3.4: orderbook depth_1pct (bid+ask) when present, else
    # volume_24h fallback, else None. The aggregator handles None as
    # "no signal → neutral weight 1.0" so cold-start markets aren't punished.
    liquidity: float | None = field(default=None)


_BTC_PRICE_RE = re.compile(r"\$\s?\d")
_BTC_VOL_RE = re.compile(r"volatilit|implied vol|\bvix\b", re.IGNORECASE)
_BTC_ABOVE_RE = re.compile(
    r"\b(?:above|exceed|exceeds|reach|reaches|hit|hits|over|surpass|surpasses|greater|higher)\b",
    re.IGNORECASE,
)
_BTC_BELOW_RE = re.compile(
    r"\b(?:below|under|beneath|dip|dips|fall|falls|drop|drops|crash|crashes|less|lower)\b",
    re.IGNORECASE,
)


_BTC_WHICH_FIRST_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s+(?:before|vs\.?|versus)\s+\$?\s?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _btc_price_direction(title: str | None) -> float | None:
    """Deterministic bull/bear polarity for an unambiguous BTC *price-level*
    contract: +1 when a YES means the price reached/exceeded a level, -1 when it
    means the price fell below one. Returns None for anything not clearly a
    price-level bet (ranges/between, volatility-index markets, symmetric
    up/down), leaving those to the model. A "which threshold first" bet
    ("hit $A before $B") is signed by which threshold YES reaches first. Guards the small local factor model,
    which occasionally inverts obvious "exceed $X" contracts (e.g. reading
    "highest price of Bitcoin exceeds $100,000" as bearish)."""
    t = title or ""
    if not _BTC_PRICE_RE.search(t) or _BTC_VOL_RE.search(t):
        return None
    wf = _BTC_WHICH_FIRST_RE.search(t)
    if wf:
        a = float(wf.group(1).replace(",", ""))
        b = float(wf.group(2).replace(",", ""))
        # "hit $A before $B" — YES reaches $A first. Reaching the LOWER threshold
        # first is a down move (bearish); the higher, up (bullish).
        return -1.0 if a < b else 1.0
    up = bool(_BTC_ABOVE_RE.search(t))
    down = bool(_BTC_BELOW_RE.search(t))
    if up == down:  # both or neither keyword -> ambiguous, defer to the model
        return None
    return 1.0 if up else -1.0


def _direction_value(
    evals: dict[str, AuditEvaluation], ir: IndexDef, title: str | None = None
) -> float:
    """The direction factor encodes -1 / 0 / +1 and flips a market's price
    contribution by what a YES resolution *means* for the index theme
    (escalation for war, price-up for BTC).

    It is the index's single ternary factor, located by TYPE so the factor id is
    free to be theme-specific — ``direction`` (war), ``btc_direction`` (BTC), … A
    hardcoded ``evals.get("direction")`` silently defaulted any index whose
    ternary factor had a different id to +1 (theme-positive) for every market,
    nullifying its bull/bear signal.

    Missing-eval semantics split by whether the index declared a ternary factor:
    an index that DID declare one treats a missing/failed eval as 0 (neutral →
    excluded downstream) — defaulting to +1 there would count an unevaluated
    market as escalation/bullish, a directional bias. Indexes with no declared
    ternary fall back to the legacy id ``direction`` and +1 when absent, which
    preserves prior behaviour for indexes that never carried one (e.g. the
    seat indexes, where every market is theme-positive by construction)."""
    declared = next((f.id for f in ir.factors if f.type == "ternary"), None)
    # A clear BTC price-level title overrides the model's polarity (see
    # _btc_price_direction); scoped to the BTC direction factor so war/others
    # are untouched.
    if declared == "btc_direction" and title is not None:
        rule = _btc_price_direction(title)
        if rule is not None:
            return rule
    e = evals.get(declared if declared is not None else "direction")
    if e is None or e.value_numeric is None:
        return 0.0 if declared is not None else 1.0
    return float(e.value_numeric)


def _gate_factor_ids(ir: IndexDef) -> list[str]:
    """Ids of factors flagged as membership gates (usually 0 or 1 of them)."""
    return [f.id for f in ir.factors if f.gate]


def _passes_gate(evals: dict[str, AuditEvaluation], gate_ids: list[str]) -> bool:
    """True if every gate factor evaluated truthy (==1).

    A market is a member only if it clears every declared on-topic gate. A
    missing or 0 gate eval fails the gate (no evidence it's on-topic → out),
    so an off-topic market can't ride in on the other factors' soft relevancy.
    Empty ``gate_ids`` (the default for every index) always passes — additive.
    """
    for fid in gate_ids:
        e = evals.get(fid)
        if e is None or e.value_numeric is None or e.value_numeric < 0.5:
            return False
    return True


def _relevancy(evals: dict[str, AuditEvaluation], ir: IndexDef) -> float:
    """Σ(value × weight) / Σ(weight) over factors that carry a relevancy weight."""
    total_weight = 0.0
    weighted_sum = 0.0
    for factor in ir.factors:
        if factor.weight is None:
            continue
        e = evals.get(factor.id)
        if e is None or e.value_numeric is None:
            continue
        # Binary 0/1 in [0,1]; score in [0,1] expected. Ternary should not be weighted.
        value = float(e.value_numeric)
        weighted_sum += value * factor.weight
        total_weight += factor.weight
    if total_weight == 0:
        return 0.0
    return weighted_sum / total_weight


def _collapse(rows: list[MarketEvaluations], ir: IndexDef) -> list[MarketEvaluations]:
    """Bucket collapse: see ``pmi_core.engine.bucket_collapser``.

    Returns the unchanged input when ``ir.aggregation.collapse.enabled`` is
    False. The collapser is date-aware (uses ``parse_bucket_date`` /
    ``strip_date_suffix``) so it correctly identifies "...on June 20" /
    "...on June 21" as siblings while leaving "Will X happen?" + "Will Y
    happen?" alone.
    """
    return collapse_for_scoring(rows, ir)


# CORR-3.4 — quantile bucket ladder. Same shape as Micah's source_weights
# (0.90 / 1.00 / 1.20 / 1.50) so the relative dampening / boosting is
# familiar; minimum sample size (4) is the smallest N where p20/p50/p80
# yield three distinct cut points after interpolation. Below that we
# return uniform 1.0, which collapses back to the pre-CORR-3.4 behaviour
# and keeps tiny indexes from being volatility-amplified by quantiles
# computed off ~2 markets.
_LIQUIDITY_MIN_SAMPLE = 4
_LIQUIDITY_BUCKETS = (
    (0.20, 0.90),   # bottom quintile — thin book, discount
    (0.50, 1.00),   # 20–50 — neutral
    (0.80, 1.20),   # 50–80 — above median, modest boost
    (1.00, 1.50),   # 80–100 — deep book, strong boost
)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (matches numpy's default 'linear' method
    and Micah's ``calculate_volume_stats``)."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    k = (n - 1) * p
    f = int(k)
    frac = k - f
    if f + 1 < n:
        return sorted_vals[f] + frac * (sorted_vals[f + 1] - sorted_vals[f])
    return sorted_vals[f]


def _liquidity_weights(
    rows: list[MarketEvaluations], ir: IndexDef
) -> tuple[dict[int, float], dict]:
    """Return ``{market_id: weight}`` per ``WeightingSpec.liquidity.method``.

    Markets with no liquidity signal (None or ≤ 0) are always weighted 1.0
    regardless of method so cold-start markets degrade gracefully instead
    of being silently zeroed-out. The second return value is a small
    breakdown dict for ``audit_pipeline_runs`` / ``ts_index_scores.breakdown``
    so an operator can see *why* a tick produced a particular score.

    Play-money markets (``is_play_money``) are deliberately excluded from the
    quantile sample: their "volume" is site credits / engagement, not real money,
    so letting them define the depth percentiles would corrupt the ladder. They
    stay full members weighted ``play_money_weight`` — venue gates the *weighting math*
    here, never *membership*, which relevance decides upstream in the selector.
    """
    method = ir.weighting.liquidity.method
    # Play-money never contributes to a score (money-type rule: shown, not scored).
    # `ir.weighting.liquidity.play_money_weight` is now inert — kept as a field for
    # backcompat but forced to 0 here so every vertical treats play-money the same.
    pm_w = 0.0
    default = {
        r.market.id: (pm_w if getattr(r.market, "is_play_money", False) else 1.0)
        for r in rows
    }
    info = {
        "method": method,
        "sample_size": 0,
        "applied": False,
    }

    if method == "none":
        return default, info

    play_money_excluded = sum(
        1 for r in rows if r.market.is_play_money and has_liquidity(r.liquidity)
    )
    if play_money_excluded:
        info["play_money_excluded"] = play_money_excluded

    samples = [
        (r.market.id, float(r.liquidity))
        for r in rows
        if has_liquidity(r.liquidity) and not r.market.is_play_money
    ]

    # Base weight for markets that do NOT enter the ladder (no positive signal):
    # weight 0 — a no-data/untraded market stays a member (shown) but does not move
    # a liquidity-weighted score (see engine/liquidity.py). Play-money keeps
    # `play_money_weight` (money-type is orthogonal to liquidity). The fallback
    # returns below still use `default` (uniform), so a book with NO positive
    # signal at all degrades to a uniform mean, not an all-zero book.
    applied_base = {
        r.market.id: (pm_w if getattr(r.market, "is_play_money", False) else 0.0)
        for r in rows
    }
    info["sample_size"] = len(samples)
    if len(samples) < _LIQUIDITY_MIN_SAMPLE:
        info["reason"] = f"sample_size<{_LIQUIDITY_MIN_SAMPLE}; falling back to uniform"
        return default, info

    sorted_vals = sorted(d for _, d in samples)
    if sorted_vals[0] == sorted_vals[-1]:
        info["reason"] = "no variance in liquidity; falling back to uniform"
        return default, info

    if method == "quantile":
        p20 = _percentile(sorted_vals, 0.20)
        p50 = _percentile(sorted_vals, 0.50)
        p80 = _percentile(sorted_vals, 0.80)

        def bucket(d: float) -> float:
            if d < p20:
                return _LIQUIDITY_BUCKETS[0][1]
            if d < p50:
                return _LIQUIDITY_BUCKETS[1][1]
            if d < p80:
                return _LIQUIDITY_BUCKETS[2][1]
            return _LIQUIDITY_BUCKETS[3][1]

        weights = dict(applied_base)
        for mid, d in samples:
            weights[mid] = bucket(d)
        info.update({"applied": True, "p20": p20, "p50": p50, "p80": p80})
        return weights, info

    if method == "linear":
        max_d = sorted_vals[-1]
        weights = dict(applied_base)
        # Map (0, max_d] → [0.5, 1.5] linearly so dynamic range matches the
        # quantile ladder's outer bounds.
        for mid, d in samples:
            weights[mid] = max(0.5, min(1.5, 0.5 + d / max_d))
        info.update({"applied": True, "max_d": max_d})
        return weights, info

    # Unknown method → fall back. Shouldn't happen given the IR's Literal,
    # but stays defensive in case the schema is widened.
    info["reason"] = f"unknown method '{method}'; falling back to uniform"
    return default, info


@dataclass
class AggregationResult:
    # None when the index can't produce a meaningful score this tick
    # (below ``min_components`` or zero weighted relevancy). Persisting NULL
    # rather than 0.0 prevents the API from returning a stale-looking real
    # value when the real story is "no data" (see alembic 0006).
    score: float | None
    component_count: int
    component_evaluation_ids: list[int]
    breakdown: dict


# CORR-1.8 backcompat: pre-extraction-block definitions stored in
# `core_index_definitions` declared `formula: seat_projection_sum` with the
# us_state (senate) grammar hardcoded — this spec reproduces that exactly.
_LEGACY_SEAT_EXTRACTION = ExtractionSpec(group_by="us_state")


def _extraction_inputs(
    rows: list[MarketEvaluations], ir: IndexDef, spec: ExtractionSpec
) -> tuple[
    list[tuple[int, str, float]],
    dict[int, str],
    dict[int, str | None],
    dict[int, tuple[str, str]],
    dict[int, str],
]:
    """(triples, party hints, venue map, extraction hints, extraction kinds).

    Party hints feed the candidates tier: Gamma's ``groupItemTitle`` party
    tag when present, else the index's own audited ``republican_on_yes``
    factor evaluation (1.0 ⇒ the YES side is the Republican). Extraction
    hints feed the llm_fallback tier: the value_label of the index's
    ``type: extraction`` factor (whatever its id — declarative, not a
    hardcoded factor name), validated against the spec's entity key space
    (``parse_label_for`` — e.g. real US state, plausible district number).
    Extraction kinds carry the same label's ``race_type`` so an LLM-named
    candidate leg is summed by party rather than read as a directed P(R).
    """
    # Drop election-noise markets that share a race's keywords but answer a
    # different question — primaries, nominations, county splits, margin bets,
    # state-legislature seats (race_identity.exclusion_reason). Only for the
    # partisan US grammars (sides: party); the war country grammar (sides: none)
    # has no such noise classes and is left untouched.
    exclude = spec.sides == "party"
    triples = [
        (r.market.id, r.market.title, r.last_price)
        for r in rows
        if r.last_price is not None
        and not (exclude and exclusion_reason(r.market.title, r.market.external_id))
    ]
    extraction_factor_id = next(
        (f.id for f in ir.factors if f.type == "extraction"), None
    )
    parse_label = parse_label_for(spec)
    party_hints: dict[int, str] = {}
    extraction_hints: dict[int, tuple[str, str]] = {}
    extraction_kinds: dict[int, str] = {}
    for r in rows:
        raw = r.market.raw if isinstance(r.market.raw, dict) else {}
        rep_eval = r.by_factor.get("republican_on_yes")
        party = resolve_candidate_party(
            raw.get("groupItemTitle"),
            float(rep_eval.value_numeric)
            if rep_eval is not None and rep_eval.value_numeric is not None
            else None,
        )
        if party is not None:
            party_hints[r.market.id] = party
        if extraction_factor_id is None:
            continue
        ext_eval = r.by_factor.get(extraction_factor_id)
        if ext_eval is not None and ext_eval.value_numeric == 1.0:
            ext = parse_label(ext_eval.value_label)
            if ext is not None:
                extraction_hints[r.market.id] = ext
                kind = extraction_race_type(ext_eval.value_label)
                if kind is not None:
                    extraction_kinds[r.market.id] = kind
    venue_by_market = {r.market.id: r.market.venue for r in rows}
    return triples, party_hints, venue_by_market, extraction_hints, extraction_kinds


def _apply_cycle_gates(observations, spec, rows):
    """Filter observations to the index's declared election cycle (shared gate).

    No-op when ``spec.cycle`` is None (cycle-agnostic). Otherwise two gates,
    identical for every formula that groups by race:

    * gate 1 (per-market): drop observations whose ``election_cycle`` is a KNOWN
      different year — the classifier reads only the (often yearless) title, so a
      2028 kalshi leg would otherwise land in the 2026 index. Undeterminable
      cycle is kept (fail open); gate 2 is the backstop.
    * gate 2 (entity): for us_state / us_governor keep only the states the
      ``race_calendar`` lists as contested that cycle; us_house_district (all 435
      up every even year) and country return None → no calendar gate.
    """
    if spec.cycle is None:
        return observations
    cycle_by_market = {
        r.market.id: election_cycle(
            r.market.venue, r.market.title, r.market.external_id, r.market.closes_at
        )
        for r in rows
    }
    observations = [
        o for o in observations if cycle_by_market.get(o.market_id) in (None, spec.cycle)
    ]
    allowed = allowed_states_for(spec.group_by, spec.cycle)
    if allowed is not None:
        observations = [o for o in observations if state_code(o.key) in allowed]
    return observations


def _liquidity_by_market(rows: list[MarketEvaluations]) -> dict[int, float]:
    """``{market_id: book_depth}`` for real-money markets with a positive signal.

    Play-money markets are excluded (weight 0) so site-credit "volume" never
    dominates a race's cross-venue mean; they stay members via the simple-mean
    fallback inside reduce_races.
    """
    return {
        r.market.id: float(r.liquidity)
        for r in rows
        if has_liquidity(r.liquidity) and not r.market.is_play_money
    }


def _observations_from_tags(
    rows: list[MarketEvaluations],
    spec: ExtractionSpec,
    race_tags: dict[int, CoreMarketRaceTag],
) -> list[EntityObservation]:
    """EntityObservations built straight from the persisted race tags.

    The single classification source for election indexes — replaces the title
    re-parse (``classify_entities``) so a market is classified exactly once, in
    the ``tag-races`` job, not again here. The tag's ``kind`` + ``resolved_via``
    rebuild the tier the reduction consumes: a ``candidate`` tag is a
    candidates-tier leg (summed by party, whichever tier named it); a
    ``party_direct`` tag keeps the tier that produced it (``patterns`` or
    ``llm_fallback``), so inside one venue a deterministic party leg still
    outranks an LLM-extracted one (CORR-1.11 — before 2026-10-12 every
    party_direct tag was rebuilt as ``patterns`` and the LLM noise averaged in).
    Only markets that have a tag AND a live price contribute. Cross-venue
    reduction + the shared cycle gates run downstream unchanged, so the tag
    path and the keyword path collapse a race identically.
    """
    grammar = _GRAMMARS[spec.group_by]
    key_code = grammar.key_code
    house = spec.group_by == "us_house_district"
    out: list[EntityObservation] = []
    for r in rows:
        tag = race_tags.get(r.market.id)
        if tag is None or r.last_price is None:
            continue
        key = f"{tag.state}-{tag.district}" if house else tag.state
        via = tag.resolved_via if tag.resolved_via in ("patterns", "llm_fallback", "candidates") else None
        if tag.kind == "candidate":
            tier = "candidates"
            parsed = grammar.parse_candidate(r.market.title or "")
            label = parsed[0] if parsed else candidate_label_from_title(r.market.title)
        else:
            tier = via if via in ("patterns", "llm_fallback") else "patterns"
            label = None
        out.append(
            EntityObservation(
                market_id=r.market.id,
                key=key,
                code=key_code(key),
                party=tag.party_side,
                price=float(r.last_price),
                tier=tier,
                venue_rank=_venue_rank(r.market.venue),
                label=label,
                resolved_via=via if via != tier else None,
            )
        )
    return out


def _election_observations(
    rows: list[MarketEvaluations],
    ir: IndexDef,
    spec: ExtractionSpec,
    race_tags: dict[int, CoreMarketRaceTag] | None,
) -> tuple[list[EntityObservation], dict[int, str | None], int]:
    """(observations, venue_by_market, candidate_count) for an election formula.

    Tag path (``race_tags`` supplied) reads the persisted classification;
    keyword path (None) re-parses titles via ``classify_entities`` — the two are
    the same grammar, so the observation set matches. Cycle gates + reduce_races
    run identically after either branch.
    """
    venue_by_market = {r.market.id: r.market.venue for r in rows}
    if race_tags is not None:
        observations = _observations_from_tags(rows, spec, race_tags)
        candidate_count = sum(1 for r in rows if r.last_price is not None)
    else:
        triples, party_hints, venue_by_market, extraction_hints, kinds = _extraction_inputs(
            rows, ir, spec
        )
        observations = classify_entities(
            spec,
            triples,
            party_by_market=party_hints,
            venue_by_market=venue_by_market,
            extraction_by_market=extraction_hints,
            extraction_kinds=kinds,
        )
        candidate_count = len(triples)
    observations = _apply_cycle_gates(observations, spec, rows)
    return observations, venue_by_market, candidate_count


def _seat_projection_aggregate(
    rows: list[MarketEvaluations],
    ir: IndexDef,
    race_tags: dict[int, CoreMarketRaceTag] | None = None,
) -> AggregationResult:
    """``formula: seat_projection_sum`` — score = expected Republican seat count.

    Reuses the exact path the ``/senate-board`` endpoint uses so the top-line
    score and the board never diverge:

    1. The declared extraction stage (``ir.extraction``; CORR-1.8) collapses
       the component markets into one ``P(R wins)`` per entity — a state for
       ``group_by: us_state``, a House district for ``us_house_district`` —
       with polarity handled by preferring the R market's price or using
       ``1 - D price``. This per-entity collapse *is* the collapse for this
       formula, so the generic date-bucket collapse is skipped. Definitions
       predating the extraction block default to the us_state (senate)
       grammar they were written against.
    2. ``compute_seat_distribution`` (CORR-1.6 Poisson-binomial) folds those
       per-race Bernoullis plus the chamber's holdover seats into ``E[R seats]``.

    The ``score`` is the expected seat count (e.g. ``52.3``), NOT the 0..100
    weighted-average index — a seat-count index returns a chamber seat count.
    """
    spec = ir.extraction or _LEGACY_SEAT_EXTRACTION
    observations, venue_by_market, candidate_count = _election_observations(
        rows, ir, spec, race_tags
    )
    # One P(R) per race, MERGING every venue (party-direct averaged, candidate
    # legs summed by party, cross-venue liquidity-weighted) — the same
    # reduce_races the MAGA lean uses, so the seat count and the MAGA-derived
    # board agree on how a race's price is formed (replaces the old
    # merge_observations representative-pick, which took one venue's one market).
    reductions = reduce_races(observations, venue_by_market, _liquidity_by_market(rows))
    # Unified play-money rule: drop a race with NO real-money member (all play-
    # money) — play-money can't create a phantom contested seat. It's weight 0, so
    # this removes phantom races only; a real race's P(R) is unchanged. Same rule
    # the markets board applies to question anchors. See venues.has_real_money.
    reductions = {
        k: red
        for k, red in reductions.items()
        if has_real_money(venue_by_market.get(mid) for mid in red.market_ids)
    }
    if len(reductions) < ir.aggregation.min_components:
        return AggregationResult(
            score=None,
            component_count=len(reductions),
            component_evaluation_ids=[],
            breakdown={
                "formula": "seat_projection_sum",
                "group_by": spec.group_by,
                "reason": "below min_components",
                "contested_seats": len(reductions),
                "candidates": candidate_count,
            },
        )

    sp = ir.aggregation.seat_projection
    geometry = {
        "holdover_r": sp.holdover_r if sp else 0,
        "holdover_d": sp.holdover_d if sp else 0,
        "total_seats": sp.total_seats if sp else 100,
        "majority_threshold": sp.majority_threshold if sp else 51,
        "tie_break": sp.tie_break if sp else None,
        "correlation": sp.correlation if sp else None,
    }
    # Zero-liquidity contested seats carry only a thin/stale price. §F(a): keep it,
    # except a GROSS misread of a known running incumbent (thin gives that party
    # <25%) → the incumbent hold. Open/unlisted seats keep the thin read. See
    # _zero_liquidity_pr — this only rescues clear misreads, not every untraded seat.
    office, cycle = _office_cycle(ir.id)
    p_rs = [
        red.p_r
        if red.weight > 0
        else _zero_liquidity_pr(red.p_r, red.code, office, cycle, _n_real_venues(red))
        for red in reductions.values()
    ]
    # E[R seats] (the score) is correlation-invariant — the copula preserves
    # every marginal — so ρ only sharpens the breakdown's tail probabilities,
    # keeping the score path consistent with the /senate-board endpoint.
    dist = compute_seat_distribution(p_rs, **geometry)

    # Lineage: every evaluation behind a market that fed a contested seat's P(R).
    seat_market_ids = {mid for red in reductions.values() for mid in red.market_ids}
    component_ids = [
        e.id
        for r in rows
        if r.market.id in seat_market_ids
        for e in r.by_factor.values()
        if e.id is not None
    ]
    return AggregationResult(
        score=round(dist.expected_r_seats, 4),
        component_count=len(reductions),
        component_evaluation_ids=component_ids,
        breakdown={
            "formula": "seat_projection_sum",
            "group_by": spec.group_by,
            "expected_r_seats": round(dist.expected_r_seats, 4),
            "stdev_r_seats": round(dist.stdev_r_seats, 4),
            "p_r_majority": round(dist.p_r_majority, 6),
            "p_d_majority": round(dist.p_d_majority, 6),
            "p_r_control": round(dist.p_r_control, 6),
            "p_d_control": round(dist.p_d_control, 6),
            "n_contested": dist.n_contested,
            **geometry,
        },
    )


def _office_cycle(index_id: str) -> tuple[str | None, int]:
    """Derive (office, cycle) from a seat/lean index id — e.g.
    'us-senate-2026-republican-seats' or 'maga-house-2026' → ('senate'|..., 2026)."""
    low = index_id.lower()
    office = (
        "senate" if "senate" in low
        else "house" if "house" in low
        else "governor" if "governor" in low
        else None
    )
    m = re.search(r"20\d\d", low)
    return office, int(m.group(0)) if m else 2026


# A zero-liquidity seat's thin read is treated as a GROSS misread — and replaced
# by the incumbent-hold — only when it gives the KNOWN incumbent's party less than
# this share. Above it, the thin read is kept: it carries the race's real
# competitiveness, which a flat hold would erase (and would overstate safe and
# competitive incumbents alike to certainty). 0.25 catches the live misreads
# (a safe-D seat priced ~83% R; an R seat priced ~23% R) and nothing else.
_MISREAD_INCUMBENT_SHARE = 0.25


def _n_real_venues(red: RaceReduction) -> int:
    """How many REAL-money venues fed a race's P(R) (play-money never counts)."""
    return sum(1 for v in red.venue_counts if v and not is_play_money_venue(v))


def _zero_liquidity_pr(
    thin_pr: float, code: str, office: str | None, cycle: int, n_real_venues: int = 1
) -> float:
    """P(R) to use for a contested seat with NO real-money liquidity.

    §F(a), refined after two impact reviews. Keep the thin market read by default —
    it carries the race's competitiveness — and override to the incumbent-hold
    (R→1.0, D→0.0) ONLY for a GROSS misread: a seat with a known RUNNING incumbent
    whose thin price gives that incumbent's party < _MISREAD_INCUMBENT_SHARE. Open
    seats (incumbent term-limited / retiring / lost primary — the party can flip)
    and unlisted seats always keep the thin read.

    A "misread" can only be claimed of a SINGLE venue's stale leg. When two or
    more real-money venues independently quote the same thin price, that is
    the market's view, not a misread — and the 119th-Congress incumbency roster
    is no prior at all for a district redrawn mid-decade (2026-10-12: CA-01,
    quoted 6–13 R on six venues, jumped to 100 on a $0-volume day because the
    roster still lists its pre-Prop-50 Republican holder). Such races keep the
    thin read.

    Lookup key: house is keyed per-district (``CA-20`` — the full race code), so a
    district's own incumbent is used, not its state's; senate/governor are keyed by
    the 2-letter state.
    """
    if n_real_venues >= 2:
        return thin_pr  # corroborated across venues: evidence, not a misread
    seat_key = (code or "").strip().upper()
    if office != "house":
        seat_key = seat_key.split("-")[0]  # senate/gov: 2-letter state, drop any suffix
    if not office or is_open_seat(office, seat_key, cycle):
        return thin_pr
    party = incumbent_party(office, seat_key, cycle)
    if party not in ("R", "D"):
        return thin_pr  # unlisted seat → keep the thin read, don't flatten
    incumbent_share = thin_pr if party == "R" else 1.0 - thin_pr
    if incumbent_share < _MISREAD_INCUMBENT_SHARE:
        return 1.0 if party == "R" else 0.0  # gross misread → incumbent hold
    return thin_pr  # thin read consistent enough with the incumbent → keep it


def _rollup_mean(values_weights: list[tuple[float, float]], mode: str) -> float:
    """Fold (value, weight) pairs into one number per ``mode``.

    ``liquidity`` → weight-weighted mean, falling back to a simple mean when the
    total weight is 0 (every race carried zero liquidity signal). ``equal`` →
    simple mean, ignoring weights. Assumes a non-empty input (callers guard with
    ``min_components``).
    """
    if mode == "liquidity":
        total = sum(w for _, w in values_weights)
        if total > 0:
            return sum(v * w for v, w in values_weights) / total
    return sum(v for v, _ in values_weights) / len(values_weights)


def _entity_lean_aggregate(
    rows: list[MarketEvaluations],
    ir: IndexDef,
    race_tags: dict[int, CoreMarketRaceTag] | None = None,
) -> AggregationResult:
    """``formula: entity_lean_mean`` — per-entity partisan lean rolled to a score.

    Two layers, with different weighting on purpose (see EntityRollupSpec):

    * **Leaf (within a race)** — each entity's heat is the LIQUIDITY-weighted
      mean of its observations (book depth, the trustworthy-price signal; simple
      mean when none carry liquidity), scaled to 0–100 (100 = deep Republican).
      Multiple contracts of one race collapse here. Unlike
      ``seat_projection_sum`` there is no representative pick — every classified
      market contributes.
    * **Rollup (across races)** — governed by ``ir.aggregation.rollup``. Default
      (None) folds entities into the top-line by a liquidity-weighted flat mean
      (the original CORR-1.8b behaviour — this method is byte-identical when no
      rollup is declared). MAGA chamber indexes set ``cross_entity: equal`` so
      every race counts once, and House adds ``group_through: us_state`` for the
      district→state→national equal-weight hierarchy (50 states weigh equally,
      not 435 districts).

    Per-entity heats land in ``breakdown["entities"]`` and, when hierarchical,
    per-group heats in ``breakdown["groups"]`` — so a choropleth or a chamber
    page reads leans straight off the score row instead of re-deriving them.
    """
    spec = ir.extraction
    assert spec is not None  # guaranteed by the IR validator
    observations, venue_by_market, candidate_count = _election_observations(
        rows, ir, spec, race_tags
    )
    liquidity_by_market = _liquidity_by_market(rows)
    market_by_id = {r.market.id: r for r in rows}
    obs_by_market = {o.market_id: o for o in observations}

    # Cross-venue reduction (Stage C): one P(R) per race, MERGING every venue's
    # contracts — party-direct legs averaged, candidate legs summed by party,
    # then a liquidity-weighted mean across venues (see seat_mapping.reduce_races).
    # Replaces the old tier-precedence drop (which discarded whole venues) + flat
    # prob_r average (which mis-scored multi-candidate races, e.g. TX-2 → 0.38).
    reductions = reduce_races(observations, venue_by_market, liquidity_by_market)
    # Unified play-money rule: drop a race with NO real-money member (all play-
    # money) — play-money enriches a real race but can't create a phantom one.
    # It's weight 0, so this only removes phantom races. Same rule the markets
    # board applies to question anchors. See venues.has_real_money.
    reductions = {
        k: red
        for k, red in reductions.items()
        if has_real_money(venue_by_market.get(mid) for mid in red.market_ids)
    }

    if len(reductions) < ir.aggregation.min_components:
        return AggregationResult(
            score=None,
            component_count=len(reductions),
            component_evaluation_ids=[],
            breakdown={
                "formula": "entity_lean_mean",
                "group_by": spec.group_by,
                "reason": "below min_components",
                "entities": len(reductions),
                "candidates": candidate_count,
            },
        )

    def _contract(mid: int) -> dict | None:
        r = market_by_id.get(mid)
        if r is None:
            return None
        m = r.market
        price = float(r.last_price) if r.last_price is not None else None
        o = obs_by_market.get(mid)
        vol = getattr(m, "last_volume_24h", None)
        return {
            "market_id": mid,
            "title": m.title,
            "venue": m.venue,
            "party": o.party if o is not None else None,
            "yes_pct": round(price * 100.0, 1) if price is not None else None,
            "p_r": round(o.prob_r, 4) if o is not None else None,
            # None = "no volume data", distinct from an actual $0 traded.
            "volume_24h": float(vol) if vol is not None else None,
            "slug": getattr(m, "slug", None),
            "url": getattr(m, "url", None),
        }

    # §F(a): a race with zero real-money liquidity has no reliable price. Flag it
    # no_market so the rollup can exclude it from the top-line lean; display it at
    # its thin read, except a gross misread of a known incumbent shows the hold
    # (see _zero_liquidity_pr — no flatten-to-50, no reversing open seats).
    office, cycle = _office_cycle(ir.id)
    entities: dict[str, dict] = {}
    for key in sorted(reductions):
        red = reductions[key]
        no_market = red.weight <= 0
        heat = (
            _zero_liquidity_pr(red.p_r, red.code, office, cycle, _n_real_venues(red)) * 100.0
            if no_market
            else red.p_r * 100.0
        )
        entities[key] = {
            "code": red.code,
            "heat": round(heat, 2),
            "n_markets": len(red.market_ids),
            "weight": round(red.weight, 4),
            "no_market": no_market,
            # True when the zero-liquidity rule replaced the thin read with the
            # incumbent hold (a prior, not a price) — surfaced so a page can say so.
            "prior_applied": no_market and abs(heat - red.p_r * 100.0) > 1e-9,
            # D10: the venues' shares of this race's fold (sums to 1).
            "venue_lineage": red.venue_lineage,
            "contracts": [c for mid in red.market_ids if (c := _contract(mid))],
        }

    # Cross-race rollup (see EntityRollupSpec). The per-entity heat above is the
    # liquidity-weighted leaf; everything from here up is governed by `rollup`.
    # Default (rollup is None) → liquidity-weighted flat mean, byte-identical to
    # the pre-rollup behaviour, so the field is purely additive.
    rollup = ir.aggregation.rollup
    mode = rollup.cross_entity if rollup is not None else "liquidity"
    group_through = rollup.group_through if rollup is not None else None

    # §F(a): the top-line lean is the mean over TRADED races only — zero-liquidity
    # races (shown in the breakdown at their incumbent-hold prior) don't move it.
    # Fall back to all entities only if none carry liquidity (avoids an empty mean).
    scored_entities = {k: e for k, e in entities.items() if not e["no_market"]} or entities

    # Which venues actually FED a race's P(R) (and how many contracts) — summed
    # from the reductions, so it reflects what survived the cross-venue merge, not
    # every classified observation. Surfaces source diversity as a first-class
    # output: a multi-source race is more robust than a single-venue one.
    venue_counts: dict[str, int] = {}
    contributing_ids: set[int] = set()
    for red in reductions.values():
        contributing_ids.update(red.market_ids)
        for v, n in red.venue_counts.items():
            venue_counts[v] = venue_counts.get(v, 0) + n

    breakdown: dict = {
        "formula": "entity_lean_mean",
        "group_by": spec.group_by,
        "n_entities": len(entities),
        "n_observations": len(contributing_ids),
        "venues": dict(sorted(venue_counts.items())),
        "entities": entities,
    }

    if group_through is not None:
        # Two-step hierarchy: entities → intermediate groups → top-line, folding
        # by `mode` at BOTH steps. For MAGA House (mode=equal, group_through=
        # us_state) this is district→state(equal)→national(equal), so 50 states
        # weigh equally rather than 435 districts.
        groups: dict[str, dict] = {}
        for key, e in scored_entities.items():
            parent = rollup_parent(spec.group_by, key)
            pkey, pcode = parent if parent is not None else (key, e["code"])
            g = groups.setdefault(
                pkey, {"code": pcode, "_vw": [], "n_entities": 0, "weight": 0.0}
            )
            g["_vw"].append((e["heat"], e["weight"]))
            g["n_entities"] += 1
            g["weight"] += e["weight"]
        for pkey in groups:
            g = groups[pkey]
            g["heat"] = round(_rollup_mean(g.pop("_vw"), mode), 2)
            g["weight"] = round(g["weight"], 4)
        score = _rollup_mean([(g["heat"], g["weight"]) for g in groups.values()], mode)
        breakdown["rollup"] = {"cross_entity": mode, "group_through": group_through}
        breakdown["n_groups"] = len(groups)
        breakdown["groups"] = {k: groups[k] for k in sorted(groups)}
    else:
        score = _rollup_mean(
            [(e["heat"], e["weight"]) for e in scored_entities.values()], mode
        )
        if rollup is not None:
            breakdown["rollup"] = {"cross_entity": mode, "group_through": None}

    component_ids = [
        e.id
        for r in rows
        if r.market.id in contributing_ids
        for e in r.by_factor.values()
        if e.id is not None
    ]
    return AggregationResult(
        score=round(score, 4),
        component_count=len(entities),
        component_evaluation_ids=component_ids,
        breakdown=breakdown,
    )


def _geo_weighted_aggregate(
    rows: list[MarketEvaluations], ir: IndexDef
) -> AggregationResult:
    """``formula: geo_weighted_mean`` — per-country directed PMI, country→region→world.

    A faithful port of Micah's war index onto the declarative pipeline (W4):

    * **Leaf (per country, L2)** — the legacy ``IndexCalculator.calculate_country
      _index``: each country's heat is ``100 × Σ(score·w)/Σ(w)`` over its
      markets, where ``score`` = price for an escalation market (direction +1),
      ``1 − price`` for a peace market (−1), neutral markets (direction 0)
      excluded, and ``w`` = relevancy × the liquidity-quantile weight. This is
      algebraically the centered/signed weighted-average, so a country's heat
      matches the legacy per-country PMI (relevancy × book-depth replacing the
      old relevancy × source_weight × soon_immediate).
    * **Group (per region, L3)** — GDP/pop-weighted mean of the region's
      countries. With ``include_zero_groups`` (the war default) the mean runs
      over the region's FULL roster, scoring marketless countries 0 so they drag
      the region down by their GDP/pop weight (legacy region ``include_zero_pmi
      =True``); otherwise only scored countries count.
    * **Top (world, L4)** — GDP/pop-weighted mean of the regions that scored > 0
      (legacy world ``include_zero_pmi=False``), each region weighted by its
      full-roster economic mass. With ``group_through`` omitted the world is
      folded straight from the scored countries instead (legacy-exact world).
    """
    spec = ir.extraction
    assert spec is not None  # guaranteed by the IR validator
    collapsed = _collapse(rows, ir)
    triples, _party_hints, venue_by_market, extraction_hints, _kinds = _extraction_inputs(
        collapsed, ir, spec
    )
    observations = classify_entities(
        spec,
        triples,
        venue_by_market=venue_by_market,
        extraction_by_market=extraction_hints,
    )
    liquidity_weights, _liquidity_info = _liquidity_weights(collapsed, ir)
    market_by_id = {r.market.id: r for r in collapsed}
    gate_ids = _gate_factor_ids(ir)

    # Prop-umbrella dedup (aggregation): a Manifold MULTIPLE_CHOICE market ingests
    # each answer as its own obs sharing ONE parent URL. Counting all N answers
    # inflates a country heat and lets one prop-bet spawn dozens of per-country
    # signals (59% of war contracts came from 45 such markets). Keep only the
    # most-relevant answer per (country, url); URL-less obs each stand alone.
    _best_by_key_url: dict[tuple[str, str], tuple[int, float]] = {}
    for obs in observations:
        r = market_by_id.get(obs.market_id)
        if r is None:
            continue
        url = getattr(r.market, "url", None)
        if not url:
            continue
        rel = _relevancy(r.by_factor, ir)
        k = (obs.key, url)
        prev = _best_by_key_url.get(k)
        if prev is None or rel > prev[1]:
            _best_by_key_url[k] = (obs.market_id, rel)

    # L2 leaf: directed relevancy×liquidity weighted mean per country.
    by_key: dict[str, dict] = {}
    used_market_ids: set[int] = set()
    for obs in observations:
        r = market_by_id.get(obs.market_id)
        if r is None:
            continue
        _url = getattr(r.market, "url", None)
        if _url and _best_by_key_url.get((obs.key, _url), (obs.market_id, 0.0))[0] != obs.market_id:
            continue  # not the representative answer for this prop market in this country
        if not _passes_gate(r.by_factor, gate_ids):
            continue  # off-topic per a membership gate (e.g. directly_about_war=0)
        direction = _direction_value(r.by_factor, ir, r.market.title)
        if direction == 0:
            continue  # neutral market — excluded, as legacy did
        relevancy = _relevancy(r.by_factor, ir)
        if relevancy <= 0:
            continue
        if r.last_price is None:
            continue  # unreachable: observations only exist for priced markets
        price = r.last_price
        score = price if direction > 0 else 1.0 - price
        w = relevancy * liquidity_weights.get(obs.market_id, 1.0)
        acc = by_key.setdefault(
            obs.key, {"num": 0.0, "den": 0.0, "n": 0, "contracts": []}
        )
        acc["num"] += score * w
        acc["den"] += w
        acc["n"] += 1
        used_market_ids.add(obs.market_id)
        m = r.market
        vol = getattr(m, "last_volume_24h", None)
        acc["contracts"].append(
            {
                "market_id": obs.market_id,
                "title": m.title,
                "venue": m.venue,
                "yes_pct": round(price * 100.0, 1),
                "direction": int(direction),
                "score": round(score, 4),
                "relevancy": round(relevancy, 4),
                # None = "no volume data", distinct from an actual $0 traded.
                "volume_24h": float(vol) if vol is not None else None,
                "slug": getattr(m, "slug", None),
                "url": getattr(m, "url", None),
                "play_money": bool(getattr(m, "is_play_money", False)),
                # Its share of the country fold (relevancy × liquidity weight);
                # 0 = shown but not scored (untraded / no-data / play-money).
                "weight": round(w, 4),
            }
        )

    rollup = ir.aggregation.rollup
    assert rollup is not None  # guaranteed by the IR validator
    rel_floor = float(getattr(rollup, "min_entity_relevancy", 0.0) or 0.0)

    entities: dict[str, dict] = {}
    for slug in sorted(by_key):
        acc = by_key[slug]
        if acc["den"] <= 0:
            continue
        heat = acc["num"] / acc["den"]
        weighting = "liquidity"
        scored = [c for c in acc["contracts"] if c["weight"] > 0]
        rel_scored = sum(c["relevancy"] for c in scored)
        if rel_floor > 0 and rel_scored < rel_floor:
            # The traded set is too thin to speak for the country — one
            # low-relevancy contract alone was deciding a reading (Turkey 92 on a
            # 0.15-relevancy sanctions market). Fall back to the full real-money
            # set weighted by relevancy only (standing quotes still carry
            # information); if even that lacks the mass, the country is not
            # scored this tick rather than published on a sliver.
            real = [c for c in acc["contracts"] if not c["play_money"]]
            rel_all = sum(c["relevancy"] for c in real)
            if rel_all >= rel_floor:
                heat = sum(c["score"] * c["relevancy"] for c in real) / rel_all
                weighting = "relevancy_only"
            else:
                continue
        entities[slug] = {
            "code": slug,
            "name": country_name(slug),
            "region": region_of(slug),
            "heat": round(heat * 100.0, 2),
            "n_markets": acc["n"],
            "n_scoring": len(scored),
            "weighting": weighting,
            "contracts": acc["contracts"],
        }

    if len(entities) < ir.aggregation.min_components:
        return AggregationResult(
            score=None,
            component_count=len(entities),
            component_evaluation_ids=[],
            breakdown={
                "formula": "geo_weighted_mean",
                "group_by": "country",
                "reason": "below min_components",
                "entities": len(entities),
                "candidates": len(triples),
            },
        )

    group_through = rollup.group_through
    include_zero = rollup.include_zero_groups

    venue_counts: dict[str, int] = {}
    for o in observations:
        if o.market_id in used_market_ids:
            v = venue_by_market.get(o.market_id)
            if v:
                venue_counts[v] = venue_counts.get(v, 0) + 1

    breakdown: dict = {
        "formula": "geo_weighted_mean",
        "group_by": "country",
        "n_entities": len(entities),
        "n_observations": len(used_market_ids),
        "venues": dict(sorted(venue_counts.items())),
        "entities": entities,
        "rollup": {
            "cross_entity": "gdp_pop_weighted",
            "group_through": group_through,
            "include_zero_groups": include_zero,
            "min_entity_relevancy": rel_floor,
        },
    }

    scored_heat = {slug: e["heat"] for slug, e in entities.items()}

    if group_through == "region":
        # L3 region heat, then L4 world over regions that scored.
        scored_by_region: dict[str, list[str]] = {}
        for slug in entities:
            reg = region_of(slug)
            if reg is not None:
                scored_by_region.setdefault(reg, []).append(slug)

        groups: dict[str, dict] = {}
        for reg in sorted(scored_by_region):
            roster = countries_in_region(reg) if include_zero else scored_by_region[reg]
            weights = gdp_pop_weights(roster)
            den = sum(weights[s] for s in roster)
            num = sum(scored_heat.get(s, 0.0) * weights[s] for s in roster)
            groups[reg] = {
                "code": reg,
                "name": REGION_NAME.get(reg, reg),
                "heat": round(num / den, 2) if den > 0 else 0.0,
                "n_entities": len(roster),
                "n_scored": len(scored_by_region[reg]),
            }

        world_regions = [reg for reg, g in groups.items() if g["heat"] > 0]
        rweights = region_weights(world_regions) if world_regions else {}
        wden = sum(rweights.values())
        score = (
            sum(groups[reg]["heat"] * rweights[reg] for reg in world_regions) / wden
            if wden > 0
            else 0.0
        )
        for reg, g in groups.items():
            g["weight"] = round(rweights.get(reg, 0.0), 6)
        breakdown["n_groups"] = len(groups)
        breakdown["groups"] = {k: groups[k] for k in sorted(groups)}
    else:
        # Flat: world folded straight from scored countries (legacy-exact world).
        slugs = list(entities)
        weights = gdp_pop_weights(slugs)
        den = sum(weights[s] for s in slugs)
        score = (
            sum(scored_heat[s] * weights[s] for s in slugs) / den if den > 0 else 0.0
        )

    component_ids = [
        e.id
        for r in collapsed
        if r.market.id in used_market_ids
        for e in r.by_factor.values()
        if e.id is not None
    ]
    return AggregationResult(
        score=round(score, 4),
        component_count=len(entities),
        component_evaluation_ids=component_ids,
        breakdown=breakdown,
    )


def aggregate(
    rows: list[MarketEvaluations],
    ir: IndexDef,
    race_tags: dict[int, CoreMarketRaceTag] | None = None,
) -> AggregationResult:
    """Return a score + lineage IDs. ``score`` is None when no score can be
    computed (insufficient components or zero relevancy).

    Most indexes use ``formula: weighted_average_x_100`` → a 0..100 index.
    Structural indexes declare an extraction stage (CORR-1.8) plus a
    formula that consumes entities: ``seat_projection_sum`` → expected seat
    count via :func:`_seat_projection_aggregate`; ``entity_lean_mean`` →
    per-entity partisan heat via :func:`_entity_lean_aggregate`;
    ``geo_weighted_mean`` → per-country war PMI rolled country→region→world via
    :func:`_geo_weighted_aggregate`.

    ``race_tags`` (market_id → CoreMarketRaceTag) switches the two election
    formulas to classify from the persisted tags instead of re-parsing titles;
    None keeps the keyword/title path (war/btc/… never pass it).
    """
    if ir.aggregation.formula == "seat_projection_sum":
        return _seat_projection_aggregate(rows, ir, race_tags)
    if ir.aggregation.formula == "entity_lean_mean":
        return _entity_lean_aggregate(rows, ir, race_tags)
    if ir.aggregation.formula == "geo_weighted_mean":
        return _geo_weighted_aggregate(rows, ir)

    collapsed = _collapse(rows, ir)
    if len(collapsed) < ir.aggregation.min_components:
        return AggregationResult(
            score=None,
            component_count=0,
            component_evaluation_ids=[],
            breakdown={"reason": "below min_components", "candidates": len(collapsed)},
        )

    liquidity_weights, liquidity_info = _liquidity_weights(collapsed, ir)
    # Strike-aware moneyness re-weighting (off by default → all 1.0, no change).
    # Composes multiplicatively with liquidity; see engine/moneyness.py.
    mny_weights, mny_info = moneyness_weights(collapsed, ir)

    numerator = 0.0
    denominator = 0.0
    component_ids: list[int] = []
    # Markets that actually move the sums. Selector noise the relevancy gate
    # zeroed (e.g. kalshi baseball "WAR" under a war keyword) is a candidate,
    # not a component — count what the lineage records, so component_count
    # always matches /explain's holdings.
    contributing = 0

    # `signed_weighted_average_x_100` centers each market on a coin-flip: a YES
    # price of 0.5 carries no directional conviction, so its term is 0 (neutral)
    # rather than +0.5, which would drag a balanced book toward the bullish pole.
    # This is what makes a Bull/Bear index actually sit at 50 when sentiment is
    # balanced (a symmetric "Up or Down" market priced 0.50 is genuinely neutral).
    # The default `weighted_average_x_100` keeps the raw price term — the legacy
    # war-index behaviour, where a market's price IS its event probability and
    # 0.5 is not a natural neutral. See the senate-share / war defs.
    signed = ir.aggregation.formula == "signed_weighted_average_x_100"
    gate_ids = _gate_factor_ids(ir)
    ternary_id = next((f.id for f in ir.factors if f.type == "ternary"), None)

    skipped_no_price = 0
    skipped_no_direction = 0
    for r in collapsed:
        if not _passes_gate(r.by_factor, gate_ids):
            continue  # off-topic per a membership gate (default: no gate → no-op)
        relevancy = _relevancy(r.by_factor, ir)
        if relevancy <= 0:
            continue
        if ternary_id is not None:
            e = r.by_factor.get(ternary_id)
            if e is None or e.value_numeric is None:
                # The index declared a direction but this market has no usable
                # eval — excluding it beats counting it as +1 (directional bias)
                # or as an evaluated-neutral 0 (denominator dilution toward 50).
                skipped_no_direction += 1
                continue
        direction = _direction_value(r.by_factor, ir, r.market.title)  # -1, 0, +1
        if r.last_price is None:
            # No price snapshot → no signal. The extraction paths already drop
            # these in `_extraction_inputs`; imputing 0.5 here would inject a
            # fake 50 % reading (unsigned) or dilute the denominator (signed).
            skipped_no_price += 1
            continue
        price = r.last_price
        # Center on 0.5 for the signed formula: price 0.5 → 0, 1.0 → +1, 0.0 → -1.
        price_term = (2.0 * price - 1.0) if signed else price
        # Effective weight = liquidity × moneyness. Moneyness is 1.0 for every
        # market unless `weighting.moneyness` is enabled; a 0 weight (e.g. a
        # market with no extractable strike under moneyness mode) drops out of
        # both sums — it is not a holding, so it must not count as a component.
        weight = liquidity_weights.get(r.market.id, 1.0) * mny_weights.get(r.market.id, 1.0)
        if weight <= 0.0 and not r.market.is_play_money:
            continue  # untraded/no-data real-money: not a holding. Play-money at
            # weight 0 stays a holding (money-type rule: shown, contributes 0).
        contributing += 1
        numerator += relevancy * direction * price_term * weight
        denominator += relevancy * weight
        component_ids.extend(e.id for e in r.by_factor.values() if e.id is not None)

    if denominator == 0:
        return AggregationResult(
            score=None,
            component_count=0,
            component_evaluation_ids=component_ids,
            breakdown={
                "reason": "zero relevancy across components",
                "candidates": len(collapsed),
            },
        )

    raw = numerator / denominator
    # raw lives in roughly [-1, +1]; map to 0..100 with 50 = neutral
    score = max(0.0, min(100.0, 50.0 + raw * 50.0))

    return AggregationResult(
        score=round(score, 4),
        component_count=contributing,
        component_evaluation_ids=component_ids,
        breakdown={
            "formula": ir.aggregation.formula,
            "centered": signed,
            "raw": round(raw, 6),
            "components_contributing": contributing,
            "components_after_collapse": len(collapsed),
            "components_pre_collapse": len(rows),
            "skipped_no_price": skipped_no_price,
            "skipped_no_direction": skipped_no_direction,
            "liquidity_weighting": liquidity_info,
            "moneyness_weighting": mny_info,
        },
    )
