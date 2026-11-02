"""IndexDef IR — the in-memory shape of a declarative PMI definition.

YAML on disk → `IndexDef.model_validate(yaml.safe_load(...))` → engine consumes IR.
At P0 we accept a deliberately narrow subset: keyword/category selectors, fixed factors
with constant weights, simple liquidity-weighted aggregation with bucket collapse.
P1 adds semantic selectors, computed weights, expression formulas.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class KeywordSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["keyword"]
    terms: list[str] = Field(min_length=1)
    # Raw Postgres POSIX regexes matched against the title AS-IS (NOT
    # word-boundary-wrapped, NOT re.escaped like `terms`). For formats a literal
    # keyword can't capture — e.g. IBKR's bare House-district titles
    # "MI-13 General Election — <candidate>" (no "House" word), matched by
    # `[A-Z]{2}-[0-9]{1,2} General Election`. Keep each pattern anchored on a
    # literal substring so the pg_trgm GIN index still assists (no seq scan).
    title_patterns: list[str] = Field(default_factory=list)


class CategorySelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["category"]
    polymarket_tag: str


class SemanticSelector(BaseModel):
    """P1+ — included so YAML written today validates against a forward-compatible shape."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["semantic"]
    anchor: str
    min_similarity: float = Field(ge=0.0, le=1.0, default=0.78)


SelectorSpec = KeywordSelector | CategorySelector | SemanticSelector


class FactorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    # "extraction" (2026-06-12): structured extraction instead of a numeric
    # judgement — value_numeric is a 0/1 "did extract" flag and value_label
    # carries a canonical JSON payload (e.g. seat-race {state, party,
    # race_type}). Consumed by structural aggregators (seat_projection_sum)
    # as an LLM fallback when title patterns miss; never relevancy-weighted.
    type: Literal["binary", "ternary", "score", "extraction"]
    prompt_ref: str  # e.g. 'prompts/factors/directly_about_war-v1'
    weight: float | None = None  # None = does not participate in relevancy aggregation
    # Membership gate (additive, default off): when true, a market is excluded
    # from the index entirely if this factor evaluates 0 (or is missing) —
    # NOT merely down-weighted in the relevancy mean. Lets a single on-topic
    # binary ("directly_about_war") act as a hard filter so off-topic markets
    # that still trip other factors (e.g. a "Counter-Strike … strike" esports
    # market firing armed_conflict/kinetic) can't survive on a soft relevancy
    # score. Applies uniformly across the relevancy formulas; an index with no
    # gate factor is byte-identical to before.
    gate: bool = False

    @model_validator(mode="after")
    def _extraction_never_weighted(self) -> FactorSpec:
        if self.type == "extraction" and self.weight is not None:
            raise ValueError(
                f"factor '{self.id}': extraction factors carry structure, not a "
                "relevancy judgement — weight must be null"
            )
        if self.gate and self.type != "binary":
            raise ValueError(
                f"factor '{self.id}': gate is only meaningful for a binary "
                f"on-topic factor (got type '{self.type}')"
            )
        return self


class ExtractionSpec(BaseModel):
    """Declarative entity-extraction stage (CORR-1.8, 2026-06-12 design).

    Unifies the structural aggregation paths: the pipeline is
    selector → (extraction?) → formula. When declared, the engine groups the
    selected markets into per-entity directed observations — one entity per
    ``group_by`` key (a US state, a House district), each observation a
    P(Republican side wins) read off one market — and the formula consumes
    entities instead of raw markets. ``seat_projection_sum`` folds one
    representative Bernoulli per entity into the Poisson-binomial;
    ``entity_lean_mean`` takes a liquidity-weighted mean per entity.

    ``group_by`` names the entity key space AND the title grammar used to
    parse it (engine/extraction.py):

      * ``us_state`` — per-state 2026 *Senate* race grammar
        (engine/seat_mapping.py; one Class-II seat per state, so the state IS
        the seat)
      * ``us_house_district`` — per-district 2026 US House race grammar
        ("<State>('s) <Nth>/at-large District ... general election for the US
        House of Representatives in 2026")
      * ``us_governor`` — per-state 2026 *Governor* race grammar
        (one governorship per state, so the state IS the entity, same key
        space as ``us_state`` but a distinct title grammar — "gubernatorial /
        governor race", "election for governor of <State>")
      * ``country`` — per-country war/conflict grammar (engine/geo.py; the
        entity key is the country slug "ukraine", "north-korea"). No party
        side — direction is the index's ``direction`` ternary factor, so
        ``sides: none``. Patterns tier = the alias matcher (claims a title
        naming exactly one country); ``llm_fallback`` picks the primary
        country for bilateral titles ("Russia invade Ukraine").

    ``methods`` are the extraction tiers, strictly precedence-ordered
    regardless of list order (pattern > candidate > LLM — a deterministic
    parse always beats a hint, which always beats an LLM extraction):

      * ``patterns`` — party-direct per-venue title regexes
      * ``candidates`` — candidate-name titles, party resolved from the
        candidate label tag (groupItemTitle) or the index's audited
        ``republican_on_yes`` factor evaluation
      * ``llm_fallback`` — the index's ``type: extraction`` factor (an
        audited structured extraction for titles no pattern understands;
        strictly additive, validated against the entity key space, can never
        override a deterministic parse)

    ``sides`` declares what the two complementary sides of an entity are.
    ``party`` (R/D) collapses two partisan markets into one directed
    P(R wins) — the MAGA design. ``none`` means the entity has no opposing
    sides at extraction time (war: a country is just a key; the
    escalation/peace direction lives in the ``direction`` factor consumed by
    ``geo_weighted_mean``), so observations carry a sentinel party and the
    candidates tier (which needs R/D) must be left out of ``methods``.
    """

    model_config = ConfigDict(extra="forbid")
    group_by: Literal["us_state", "us_house_district", "us_governor", "country"]
    sides: Literal["party", "none"] = "party"
    methods: list[Literal["patterns", "candidates", "llm_fallback"]] = Field(
        default_factory=lambda: ["patterns", "candidates", "llm_fallback"],
        min_length=1,
    )
    cycle: int | None = Field(
        default=None,
        description=(
            "The even-year election cycle this index scopes to (e.g. 2026). When "
            "set, the engine (a) drops markets whose recovered election_cycle is a "
            "KNOWN different cycle — kalshi's yearless titles carry the cycle only "
            "in the ticker, so a 2028 market no longer pollutes a 2026 index — and "
            "(b) for us_state / us_governor keeps only entities the race_calendar "
            "lists as contested that cycle (us_house_district is all-435 pass-"
            "through). None = cycle-agnostic (every classified market, the "
            "pre-cycle behaviour). Markets with an UNDETERMINABLE cycle are kept "
            "(fail-open); the race_calendar gate is the backstop."
        ),
    )

    @model_validator(mode="after")
    def _check_sides(self) -> ExtractionSpec:
        # The candidates tier resolves a candidate name to a party (R/D); with
        # no party sides there is nothing for it to resolve, so it would only
        # ever drop markets. Forbid the combination rather than silently no-op.
        if self.sides == "none" and "candidates" in self.methods:
            raise ValueError(
                "extraction.sides 'none' is incompatible with the 'candidates' "
                "method (no party to resolve a candidate name to) — drop "
                "'candidates' from methods"
            )
        # The country grammar is partyless by construction.
        if self.group_by == "country" and self.sides != "none":
            raise ValueError(
                "extraction.group_by 'country' requires sides 'none' "
                "(war entities have no R/D side; direction is a factor)"
            )
        if self.cycle is not None:
            # US elections are even years; the race_calendar / cycle gate only
            # makes sense for the US partisan grammars, not the war country one.
            if self.group_by == "country":
                raise ValueError(
                    "extraction.cycle does not apply to group_by 'country' "
                    "(war has no election cycle)"
                )
            if self.cycle % 2 != 0 or not 2024 <= self.cycle <= 2040:
                raise ValueError(
                    f"extraction.cycle must be an even election year in "
                    f"2024..2040, got {self.cycle}"
                )
        return self


class LiquidityWeighting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["quantile", "linear", "none"] = "quantile"
    # Multiplier applied to play-money (e.g. Manifold) markets' weight. Real-money
    # markets keep their depth-quantile weight (0.9-1.5); play-money markets - whose
    # "volume" is site credits, not USD - are scaled by this instead of the flat 1.0
    # neutral, so real-money signal drives a country's heat where it exists while
    # play-money stays as coverage backbone (membership unchanged). 1.0 = no discount.
    play_money_weight: float = 1.0


class CollapseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_spread_days: int = 30
    representative: Literal["max_probability", "highest_liquidity", "newest"] = "max_probability"


class SeatProjectionSpec(BaseModel):
    """Chamber geometry for seat-count / balance-of-power indexes (CORR-1.2).

    Only consulted by seat-projection formulas and the senate-board endpoint
    (engine/seat_distribution.py). A US Senate is 100 seats = ~33 contested
    (Class II, on the 2026 ballot) + ~67 holdover (Class I + III, not on the
    ballot). The holdover seats are constants added to every realization of
    the Poisson-binomial seat distribution; ``total_seats`` derives the other
    party's count, and ``majority_threshold`` is the tail boundary (51 for an
    outright Senate majority; pass total/2 to model a VP tie-break as control).

    The House is the natural zero-holdover case: all 435 seats are up every
    cycle, so ``holdover_r = holdover_d = 0`` and ``total_seats = 435``,
    ``majority_threshold = 218``.
    """

    model_config = ConfigDict(extra="forbid")
    total_seats: int = Field(default=100, ge=1)
    majority_threshold: int = Field(default=51, ge=1)
    holdover_r: int = Field(default=0, ge=0)
    holdover_d: int = Field(default=0, ge=0)
    # Which party wins a tied chamber (the VP's party for the US Senate; None
    # for chambers with no tie-break, e.g. the odd-sized House). Drives the
    # p_r_control / p_d_control fields: the tie-break party controls at
    # majority_threshold - 1 seats.
    tie_break: Literal["R", "D"] | None = None
    # National-swing correlation ρ across the contested races (single-factor
    # Gaussian copula). None / 0 → independent Poisson-binomial; ρ > 0 fattens
    # the chamber-control tails so they agree with a direct control market.
    # Fit offline with `pmi-core calibrate-correlation` and pin the value here
    # (bump the index version) so scoring stays deterministic + auditable.
    correlation: float | None = Field(default=None, ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _check_seat_arithmetic(self) -> SeatProjectionSpec:
        if self.holdover_r + self.holdover_d > self.total_seats:
            raise ValueError(
                f"holdover_r + holdover_d ({self.holdover_r + self.holdover_d}) "
                f"exceeds total_seats ({self.total_seats})"
            )
        if self.majority_threshold > self.total_seats:
            raise ValueError(
                f"majority_threshold ({self.majority_threshold}) "
                f"exceeds total_seats ({self.total_seats})"
            )
        return self


class EntityRollupSpec(BaseModel):
    """How an entity formula rolls per-entity leaves up to the top-line score.

    The per-entity (per-race / per-country) leaf is the formula's own
    liquidity-weighted mean of that entity's observations. This spec only
    governs what happens ABOVE the leaf, across entities:

      * ``cross_entity`` — the cross-entity weighting. ``equal`` = simple mean
        (every entity counts once, the MAGA design); ``liquidity`` = weight
        each entity by its summed liquidity (the original CORR-1.8b behaviour,
        kept as the default so the field is purely additive — omitting
        ``rollup`` reproduces it byte-for-byte); ``gdp_pop_weighted`` = weight
        each entity by 65 % GDP / 35 % population (the war-index geo rollup;
        only valid for ``group_by: country``, weights from engine/geo.py).
      * ``group_through`` — an optional intermediate level. When set, entities
        are first folded into these groups (by ``cross_entity``) and the groups
        are then folded into the top-line (also by ``cross_entity``), so a
        two-step hierarchy weighs the intermediate level rather than the
        leaves. ``us_state`` rolls ``us_house_district`` leaves up by state
        (50 states weigh equally instead of 435 districts). ``region`` rolls
        ``country`` leaves up by the 6-region taxonomy (country→region→world).
      * ``include_zero_groups`` — only ``geo_weighted_mean``. When true, the
        group layer (region) averages over its FULL country roster, scoring
        absent countries as 0 so they drag the region down by their GDP/pop
        weight (legacy ``include_zero_pmi=True`` for regions); the top layer
        (world) still excludes 0-score groups (legacy world's
        ``include_zero_pmi=False``).
    """

    model_config = ConfigDict(extra="forbid")
    cross_entity: Literal["equal", "liquidity", "gdp_pop_weighted"] = "liquidity"
    group_through: Literal["us_state", "region"] | None = None
    include_zero_groups: bool = False
    # geo_weighted_mean only (2026-10-12): the minimum summed relevancy the
    # TRADED (weight > 0) contracts of an entity must carry before their
    # liquidity-weighted mean is published as that entity's reading. Below it
    # the leaf falls back to a relevancy-only mean over every real-money
    # contract (standing quotes included); below it even then, the entity is
    # not scored this tick. 0 (default) = off — the pre-2026-10-12 behaviour,
    # where one 0.15-relevancy contract could decide a country on its own.
    min_entity_relevancy: float = Field(default=0.0, ge=0.0, le=1.0)


class AggregationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collapse: CollapseSpec = Field(default_factory=CollapseSpec)
    min_components: int = 1
    formula: str = "weighted_average_x_100"  # P0 stub; P1 parses real expressions
    # CORR-1.2 — chamber geometry; only meaningful for seat-projection indexes.
    seat_projection: SeatProjectionSpec | None = None
    # Cross-race rollup shape; only consulted by ``formula: entity_lean_mean``.
    # None → liquidity-weighted flat mean (the original behaviour), so this is
    # a purely additive field. MAGA chamber indexes set ``cross_entity: equal``
    # (+ ``group_through: us_state`` for House) to get the equal-weight
    # district→state→national hierarchy.
    rollup: EntityRollupSpec | None = None


class MoneynessWeighting(BaseModel):
    """Strike-aware re-weighting for price-ladder indexes (BTC Bull/Bear v4).

    An "above $X" market's YES price is P(spot_T > X): it bundles *where spot is*
    (moneyness) with *directional conviction*. Under the signed formula a far-OTM
    strike (X ≫ spot) is correctly priced ~0 yet contributes a spurious bearish
    ``2·0.05−1 ≈ −0.9``. A strike ladder lists far more strikes above spot than a
    book trades below it, so that structural skew — not sentiment — dragged BTC v3
    to ~16 (the def's KNOWN RESIDUAL).

    When enabled, each market is weighted by a Gaussian kernel in log-moneyness
    ``ln(strike / spot)`` centred on the *ladder-implied spot* — the strike where
    interpolated P(YES) crosses 0.5, read off the ladders themselves (no external
    price feed, so the offline / $0 cache-hit path is preserved). Near-the-money
    strikes, where price encodes genuine up/down conviction, dominate; deep OTM/ITM
    strikes that only encode distance-from-spot decay to ~0. A balanced book then
    reads 50 and the index moves on real directional pricing.

    Opt-in (``enabled: false`` by default) and composes multiplicatively with
    ``liquidity`` — an index that omits the block is byte-identical to before. See
    :mod:`pmi_core.engine.moneyness`.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # Gaussian kernel width in natural-log moneyness. 0.05 → a strike 5% from spot
    # keeps weight e^-0.5≈0.61, 15% → e^-4.5≈0.011 (≈$3k / $9k bands at $60k spot).
    bandwidth: float = Field(default=0.05, gt=0.0)
    # Minimum priced "above $X" strikes in one ladder (a venue + resolution-date
    # group) before its implied spot is trusted as an input to the global spot
    # estimate. Smaller ladders don't vote on spot; their markets are still
    # weighted, by the global spot. 4 = smallest N giving a stable 0.5 crossing.
    min_ladder_size: int = Field(default=4, ge=2)


class WeightingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    liquidity: LiquidityWeighting = Field(default_factory=LiquidityWeighting)
    # Strike-aware moneyness re-weighting (price-ladder indexes; default off →
    # purely additive). See MoneynessWeighting / engine/moneyness.py.
    moneyness: MoneynessWeighting = Field(default_factory=MoneynessWeighting)


class PublishSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cadence: Literal["real_time", "hourly", "every_2h", "daily"] = "every_2h"
    channels: list[Literal["api", "websocket", "webhook"]] = Field(default_factory=lambda: ["api"])


class IndexDef(BaseModel):
    """Top-level declarative PMI definition."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(ge=1)
    title: str
    owner: str | None = None

    # Which `core_markets.venue` values this index draws from. `None` (the
    # default, and what you get by omitting `venues:`) means NO venue
    # restriction — membership is decided purely by the selectors (keyword /
    # semantic / category) and the Tier 0 cosine gate, which is venue-agnostic:
    # relevance, not the trading venue, decides whether a market belongs. Declare
    # an explicit list ONLY to scope for a *structural* reason — e.g. the
    # seat-count indexes restrict to the venues whose titles parse into per-seat
    # P(party wins), since markets that can't be mapped would just burn factor
    # evals.
    # CORR-3.12 introduced this as a hard membership filter defaulting to
    # ["polymarket"]; it was demoted to optional venue *scoping* once relevance
    # became the membership rule.
    venues: list[str] | None = Field(default=None, min_length=1)
    # Whether play-money markets (`core_markets.is_play_money`, e.g. Manifold)
    # may be members. Default False: real-money venues all join by relevance, but
    # play-money stays out — not for relevance reasons (a Manifold war market IS
    # about war) but because play-money venues carry ~100× the market count and,
    # under count-based aggregation, would swamp the score regardless of the
    # liquidity-weight guard. An index whose purpose is cross-venue-incl-play
    # sentiment opts in with `include_play_money: true`; the liquidity ladder
    # still excludes those markets' fake volume (see aggregator). This is a single
    # money-type bit, NOT a venue whitelist — it does not re-introduce per-venue
    # membership lists.
    include_play_money: bool = Field(default=False)
    # CORR-2.6: per-index cap on selected markets. None → the global default
    # (`settings.selector_max_markets`, 500). The selector logs when it
    # saturates the cap so silent truncation is at least visible.
    max_markets: int | None = Field(default=None, ge=1)
    # Membership quality gate: drop markets whose latest 24h volume is below this
    # (NULL volume counts as untraded → dropped). None (default) → no gate, so
    # every existing index is unchanged. Set it for dense strike-ladder venues
    # (kalshi BTC) where most strikes never trade yet carry a stale last_price of
    # 0 that the signed formula reads as strong bearish conviction; gating runs
    # BEFORE the max_markets cap, so the cap fills with *traded* markets instead
    # of being spent on untraded zeros. A traded but genuinely ~0-priced OTM strike
    # still counts (it has real volume) — this removes untraded noise, not OTM signal.
    min_volume_24h: float | None = Field(default=None, ge=0.0)
    # Negative keyword filter (additive, default empty → every existing index
    # unchanged). Any market whose title word-boundary-matches ANY exclude term is
    # dropped from membership — applied to BOTH the keyword and semantic selector
    # paths, BEFORE the max_markets cap, so the cap fills with genuine members
    # instead of noise. Motivation: a war index's keyword ("strike"→Counter-Strike)
    # and semantic anchor (war shares "attack/invasion/shots" vocabulary with
    # sports/esports in embedding space) both pull heavy sports/esports noise that
    # otherwise consumes the cap and starves real country-war markets. Same
    # \\m..\\M word-boundary matching as `terms` — exclude the SPORT token
    # ("counter-strike", "dota", "fifa"), never the shared war token ("strike").
    exclude_terms: list[str] = Field(default_factory=list)

    selectors: list[SelectorSpec] = Field(min_length=1)
    factors: list[FactorSpec] = Field(min_length=1)
    # CORR-1.8: optional declarative extraction stage — selector → extraction
    # → formula. Required by `formula: entity_lean_mean`; `formula:
    # seat_projection_sum` defaults to the us_state (senate) grammar when the
    # block is absent so pre-CORR-1.8 definitions stored in
    # `core_index_definitions` keep validating and scoring byte-identically.
    extraction: ExtractionSpec | None = None
    weighting: WeightingSpec = Field(default_factory=WeightingSpec)
    aggregation: AggregationSpec = Field(default_factory=AggregationSpec)
    publish: PublishSpec = Field(default_factory=PublishSpec)

    @model_validator(mode="after")
    def _check_relevancy_weights(self) -> IndexDef:
        weighted = [f for f in self.factors if f.weight is not None]
        if not weighted:
            raise ValueError("at least one factor must have a weight (relevancy participation)")
        total = sum(f.weight or 0 for f in weighted)
        if total <= 0:
            raise ValueError("sum of factor weights must be positive")
        return self

    @model_validator(mode="after")
    def _check_extraction_wiring(self) -> IndexDef:
        formula = self.aggregation.formula
        extraction_formulas = (
            "seat_projection_sum",
            "entity_lean_mean",
            "geo_weighted_mean",
        )
        if self.extraction is not None and formula not in extraction_formulas:
            raise ValueError(
                f"extraction block declared but formula '{formula}' never consumes "
                f"entities — use one of {extraction_formulas} or drop the block"
            )
        if formula in ("entity_lean_mean", "geo_weighted_mean") and self.extraction is None:
            # No legacy defs for these, so unlike seat_projection_sum there is
            # no default grammar to grandfather in.
            raise ValueError(f"formula '{formula}' requires an extraction block")
        if (
            formula == "geo_weighted_mean"
            and self.extraction is not None
            and self.extraction.group_by != "country"
        ):
            raise ValueError(
                "formula 'geo_weighted_mean' needs extraction.group_by "
                f"'country' (got '{self.extraction.group_by}')"
            )
        if formula == "geo_weighted_mean":
            rollup = self.aggregation.rollup
            if rollup is None or rollup.cross_entity != "gdp_pop_weighted":
                raise ValueError(
                    "formula 'geo_weighted_mean' requires aggregation.rollup "
                    "with cross_entity 'gdp_pop_weighted'"
                )
        if (
            self.extraction is not None
            and "llm_fallback" in self.extraction.methods
            and not any(f.type == "extraction" for f in self.factors)
        ):
            raise ValueError(
                "extraction.methods includes 'llm_fallback' but no factor has "
                "type 'extraction' — declare one (or drop the method) so the "
                "tier isn't silently inert"
            )
        return self

    @model_validator(mode="after")
    def _check_rollup_wiring(self) -> IndexDef:
        rollup = self.aggregation.rollup
        if rollup is None:
            return self
        formula = self.aggregation.formula
        if formula not in ("entity_lean_mean", "geo_weighted_mean"):
            raise ValueError(
                "aggregation.rollup is only consumed by formula "
                f"'entity_lean_mean' / 'geo_weighted_mean', not '{formula}' — "
                "drop the rollup block or switch the formula"
            )
        group_by = self.extraction.group_by if self.extraction else None
        # group_through names an intermediate key level the leaf keys must nest
        # under. us_state only nests us_house_district keys ("California-20" →
        # "California"); region only nests country keys ("ukraine" → "europe").
        # State-keyed grammars (us_state / us_governor) have no sub-level to
        # fold, so a two-step rollup there would be a no-op hiding a mistake.
        if rollup.group_through == "us_state" and group_by != "us_house_district":
            raise ValueError(
                "rollup.group_through 'us_state' needs extraction.group_by "
                f"'us_house_district' (got '{group_by}') — only district keys "
                "fold up into states"
            )
        if rollup.group_through == "region" and group_by != "country":
            raise ValueError(
                "rollup.group_through 'region' needs extraction.group_by "
                f"'country' (got '{group_by}') — only country keys fold up "
                "into regions"
            )
        # gdp_pop_weighted is the geo rollup's weighting — only geo_weighted_mean
        # implements it (entity_lean_mean's _rollup_mean knows equal/liquidity
        # only), and it needs the country GDP/pop table.
        if rollup.cross_entity == "gdp_pop_weighted" and formula != "geo_weighted_mean":
            raise ValueError(
                "rollup.cross_entity 'gdp_pop_weighted' is only implemented by "
                f"formula 'geo_weighted_mean', not '{formula}'"
            )
        # include_zero_groups only changes geo_weighted_mean's region denominator.
        if rollup.include_zero_groups and formula != "geo_weighted_mean":
            raise ValueError(
                "rollup.include_zero_groups is only consumed by formula "
                f"'geo_weighted_mean', not '{formula}'"
            )
        if rollup.min_entity_relevancy and formula != "geo_weighted_mean":
            raise ValueError(
                "rollup.min_entity_relevancy is only consumed by formula "
                f"'geo_weighted_mean', not '{formula}'"
            )
        return self


def load_index_def(path: str | Path) -> tuple[IndexDef, str, str]:
    """Read a YAML file and return (parsed IR, raw_text, sha256(raw_text))."""
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    ir = IndexDef.model_validate(data)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return ir, raw, digest
