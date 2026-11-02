"""Pydantic response shapes — stable contract for pmi-web + MCP server."""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class IndexSummary(BaseModel):
    """Public shape — `id` is the slug (matches MCP convention in CLAUDE.md §8)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Map CoreIndexDefinition.index_id → response field `id`.
    id: str = Field(validation_alias=AliasChoices("index_id", "id"))
    version: int
    title: str
    is_current: bool
    owner: str | None = None
    yaml_sha256: str
    effective_from: datetime


class ScoreEnvelope(BaseModel):
    """Generic envelope: `summary` + `data` so MCP can hand it to an agent unchanged."""

    summary: str
    data: ScorePayload


class ScorePayload(BaseModel):
    index_id: str
    version: int
    as_of: datetime
    # None when the index couldn't produce a score this tick (below
    # min_components / zero relevancy). The pipeline persists NULL rather
    # than 0.0 so the UI shows "no data" instead of a stale-looking real
    # value. See ts_index_scores migration 0006.
    score: float | None
    # What the formula aggregated over — seats, countries, or markets depending
    # on it. Only `market_count` is a contract count for every index.
    component_count: int
    # Distinct contracts and venues behind `component_evaluation_ids`, venues
    # most-contributing first. Zero/empty when the reading has no lineage.
    market_count: int = 0
    venues: list[str] = []
    computed_at: datetime
    breakdown: dict | None


class HistoryPoint(BaseModel):
    as_of: datetime
    score: float | None
    component_count: int


class HistoryPayload(BaseModel):
    index_id: str
    version: int
    points: list[HistoryPoint]


class HistoryEnvelope(BaseModel):
    summary: str
    data: HistoryPayload


# --------------------------------------------------------------------------
# Job queue + durable workflows (CORR-4.6 / CORR-8.1, Postgres-backed).
# The §3.2 on-demand path returns 202 + a JobEnvelope when the cached score
# (latest ts_index_scores row) is staler than the caller's max_age_s.
# --------------------------------------------------------------------------


class JobPayload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Field name is `job_id` in responses; ORM attr is `id`.
    job_id: int = Field(validation_alias=AliasChoices("id", "job_id"))
    name: str
    status: str  # queued | running | succeeded | failed
    attempts: int
    max_attempts: int
    enqueued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict | None = None


class JobEnvelope(BaseModel):
    summary: str
    data: JobPayload


class WorkflowRunPayload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_run_id: int = Field(validation_alias=AliasChoices("id", "workflow_run_id"))
    workflow: str
    status: str  # queued | running | succeeded | failed | cancelled
    args: dict
    steps_done: int
    steps_total: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict | None = None


class WorkflowRunEnvelope(BaseModel):
    summary: str
    data: WorkflowRunPayload


class SourceHealthRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    source: str
    status: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    records_24h: int | None
    expected_records_24h: int | None
    updated_at: datetime


class ExplainComponent(BaseModel):
    market_id: int
    title: str
    last_price: float | None
    relevancy: float
    direction: float
    factors: dict[str, float | str | None]
    # Venue (exchange) the market trades on, e.g. "polymarket" / "kalshi", and
    # its latest 24h volume — surfaced so holdings cards show the real exchange
    # + traded volume instead of a hard-coded placeholder.
    venue: str | None = None
    volume_24h: float | None = None
    # Cross-venue peer group (engine/peer_grouping.py): components sharing an
    # id are the SAME question listed on different venues — the UI folds them
    # into one holding card with stacked venue chips. Singletons carry their
    # own market_id.
    peer_group_id: int | None = None


class ExplainPayload(BaseModel):
    index_id: str
    version: int
    as_of: datetime
    score: float | None
    components: list[ExplainComponent]


# --------------------------------------------------------------------------
# Senate board (SHIP-2.5) — drives the design's senate seat-board UI
# (pmi-new-frontend/views-senate.jsx). Computed from the CORR-1.6
# Poisson-binomial seat distribution over the index's component markets.
#
# STEP 1 SCOPE (contract + happy path): the *distribution* fields
# (p_r_majority / p_d_majority / expected_r_seats / counts / series_14d) are
# REAL — derived from component market prices via
# `pmi_core.engine.seat_distribution`. The per-race *attribution* fields
# (state / matchup / incumbent_party / delta_14d / contracts / exchanges) are
# best-effort and mostly null until CORR-1.3 (condition_id grouping) gives a
# proper market→seat mapping. `prob_by_state` is therefore empty until then.
#
# Field names are snake_case to match the rest of pmi-api; the pmi-web
# `lib/types.ts` adapter maps them to the board's camelCase
# (p_r_majority → pmiGOPMajority, etc.) in SHIP-2.5 (b).
# --------------------------------------------------------------------------


class SenateRace(BaseModel):
    """One contested seat. ``prob_r`` is P(Republican wins) as a 0–100 pct."""

    market_id: int
    title: str
    prob_r: float
    band: str
    volume_24h: float | None = None
    # CORR-1.3 attribution. state = title parse; matchup = candidate names from
    # groupItemTitle (null while the race only has generic party labels);
    # delta_14d = P(R) change vs the snapshot ≥14d prior (null without history);
    # contracts / exchanges = the seat's underlying per-party markets.
    state: str | None = None
    matchup: str | None = None
    # Still null — no ingested source carries incumbency yet.
    incumbent_party: str | None = None
    delta_14d: float | None = None
    contracts: int | None = None
    exchanges: list[str] = Field(default_factory=list)
    # §F(a): no real-money liquidity behind the race this tick — prob_r is the
    # standing quotes (untraded) or, when prior_applied, the incumbent-hold
    # prior rather than a traded price. None on pre-2026-10-12 cache rows.
    untraded: bool | None = None
    prior_applied: bool | None = None


class BoardTrendPoint(BaseModel):
    date: str               # ISO hour, e.g. "2026-07-14T13:00:00+00:00"
    value: float            # P(GOP control) % at that hour


class RaceTrendPayload(BaseModel):
    # One seat's P(R) history off the board cache (D10-lite; seat pages).
    index_id: str
    seat_key: str           # "NC" (senate/governor) or "AK-01" (house district)
    days: int
    points: list[BoardTrendPoint]


class RaceTrendEnvelope(BaseModel):
    summary: str
    data: RaceTrendPayload


class SenateBoardPayload(BaseModel):
    index_id: str
    version: int
    as_of: datetime

    # Balance-of-power (CORR-1.6), expressed 0–100 for the board tiles.
    # *_majority = outright (>= majority_threshold seats); *_control folds in
    # the tie-break (the VP's party controls a tied Senate at threshold - 1).
    # The board tiles show control; majority stays for API consumers.
    p_r_majority: float
    p_d_majority: float
    p_r_control: float
    p_d_control: float
    tie_break: str | None = None
    # National-swing correlation ρ used for the tails (None → independent).
    correlation: float | None = None
    expected_r_seats: float
    stdev_r_seats: float
    total_seats: int
    majority_threshold: int
    holdover_r: int
    holdover_d: int

    # Seat tally — 7 bands (safe-d … safe-r) incl. holdover folded into safes.
    counts: dict[str, int]
    d_secured: int
    r_secured: int
    tossups: int
    n_contested: int

    races: list[SenateRace]
    prob_by_state: dict[str, float]
    # ``None`` slots are ticks where the index produced no score (below
    # min_components etc.); the board renders them as gaps rather than 0.
    series_14d: list[float | None]
    # Hourly P(GOP control) history with timestamps — drives the board trend's
    # hover tooltip. Computed live from this board's own ts_page_cache history;
    # empty on cached payloads written before this field existed.
    control_series: list[BoardTrendPoint] = []


class SenateBoardEnvelope(BaseModel):
    summary: str
    data: SenateBoardPayload


# --------------------------------------------------------------------------
# MAGA-by-state (Task #6) — drives the National MAGA Index choropleth + State
# detail view. Per-state partisan lean derived on demand from the partisan
# general-election race markets already ingested (pmi_core.engine.state_lean),
# rather than 50 standalone state index definitions.
# --------------------------------------------------------------------------


class StateLeanRow(BaseModel):
    state: str            # canonical name, e.g. "North Carolina"
    state_code: str       # 2-letter code, e.g. "NC"
    heat: float           # 0–100, 100 = deep Republican
    n_markets: int        # contributing race markets
    offices: list[str]    # distinct offices (senate / governor / house)
    volume_24h: float
    exchanges: list[str] = []  # distinct venues backing this state's races
    # Per-office heat, equal-weight over the state's races — matches the engine's
    # cross_entity: equal rollup (deliberately NOT liquidity-weighted). Server
    # truth for the by-chamber map scoping; {} on pre-field cache rows (D7).
    office_heat: dict[str, float] = {}
    delta_14d: float | None = None  # state heat vs ~14d-old snapshot


class MagaChamberStat(BaseModel):
    # Per-chamber national PMI + live contract total. national_heat is the
    # equal-weight average of the three chambers' pmi.
    pmi: float | None
    n_markets: int
    delta_24h: float | None = None  # chamber pmi vs ~24h-old snapshot (D7)
    delta_14d: float | None = None  # …vs ~14d-old


class MagaByStatePayload(BaseModel):
    as_of: datetime
    # Keyed by 2-letter state code; states with no recognised market are absent.
    states: dict[str, StateLeanRow]
    # Volume-weighted national lean across all contributing markets (0–100).
    national_heat: float | None
    n_states: int
    n_markets: int
    # Distinct contributing venues across the vertical (for the exchange icons).
    exchanges: list[str] = []
    # Per-chamber national PMI + contract totals (keys: senate/house/governor);
    # empty on caches predating the field. By-chamber tabs read these.
    chambers: dict[str, MagaChamberStat] = {}
    # National race-attributed 24h book + Δ vs the snapshot's own history (D7);
    # None on pre-field cache rows.
    volume_24h: float | None = None
    delta_24h: float | None = None
    delta_14d: float | None = None


class MagaByStateEnvelope(BaseModel):
    summary: str
    data: MagaByStatePayload


# --- State detail: per-chamber groups + contributing contracts ---------------


class MagaRaceContractRow(BaseModel):
    market_id: int
    title: str
    venue: str
    yes_pct: float          # the market's own latest YES probability, 0–100
    p_r: float              # directed P(Republican wins), 0–1
    volume_24h: float | None
    slug: str | None
    url: str | None = None


class MagaVenueLineage(BaseModel):
    # D10: one venue's P(R) and its normalized share of the race's cross-venue
    # fold (shares sum to 1; Σ share·p_r reconciles to the race heat/100).
    venue: str | None = None
    p_r: float
    share: float


class MagaGroupRow(BaseModel):
    state: str
    state_code: str
    office: str             # senate / governor / house
    district: int | None = None
    heat: float             # 0–100, 100 = deep Republican
    n_markets: int
    volume_24h: float
    base_question: str
    contracts: list[MagaRaceContractRow]
    exchanges: list[str] = []  # distinct venues backing this race
    # Party holding the seat pre-election ("R"|"D"), filled at serve time from the
    # static incumbency config (506-seat roster incl. per-district house).
    incumbent_party: str | None = None
    delta_14d: float | None = None  # race heat vs ~14d-old snapshot (D7)
    venue_lineage: list[MagaVenueLineage] = []  # D10; [] on pre-field caches
    # §F(a): zero real-money liquidity this tick (heat = standing quotes), and
    # whether the incumbent-hold prior replaced the thin read. None pre-field.
    no_market: bool | None = None
    prior_applied: bool | None = None


class MagaStateDetailPayload(BaseModel):
    state: str
    state_code: str
    heat: float
    n_markets: int
    volume_24h: float
    offices: list[str]
    groups: list[MagaGroupRow]


class MagaStateDetailEnvelope(BaseModel):
    summary: str
    data: MagaStateDetailPayload


class MagaGroupsPayload(BaseModel):
    as_of: datetime
    groups: list[MagaGroupRow]
    n_states: int
    n_markets: int


class MagaGroupsEnvelope(BaseModel):
    summary: str
    data: MagaGroupsPayload


class MagaTrendPoint(BaseModel):
    date: str               # ISO date, e.g. "2026-06-07"
    value: float            # state heat 0–100 that day


class MagaTrendsPayload(BaseModel):
    state_code: str
    days: int
    points: list[MagaTrendPoint]


class MagaTrendsEnvelope(BaseModel):
    summary: str
    data: MagaTrendsPayload


class MagaNationalTrendsPayload(BaseModel):
    days: int
    points: list[MagaTrendPoint]


class MagaNationalTrendsEnvelope(BaseModel):
    summary: str
    data: MagaNationalTrendsPayload


class MagaLastUpdatedPayload(BaseModel):
    generated_at: datetime | None   # None when no race-market snapshot exists


class MagaLastUpdatedEnvelope(BaseModel):
    summary: str
    data: MagaLastUpdatedPayload


# --------------------------------------------------------------------------
# War index (country → region → world) — read API over ts_page_cache
# --------------------------------------------------------------------------


class WarTopContract(BaseModel):
    # A roster-level "deepest contract" — enough for the map hover panel / movers
    # without fetching the per-country detail blob.
    market_id: int | None = None
    title: str | None = None
    venue: str | None = None
    yes_pct: float | None = None
    url: str | None = None
    play_money: bool | None = None   # site credits, not dollars (manifold/metaculus)


class WarConcentration(BaseModel):
    top4_share: float | None = None       # share of world 24h book in the top-4 countries
    bottom30_share: float | None = None   # …in the bottom-30
    n_countries_priced: int | None = None


class WarBreadth(BaseModel):
    up: int = 0     # countries pricing higher over 7d
    down: int = 0   # …lower
    flat: int = 0   # …unchanged (±0.05)


class WarCountryRow(BaseModel):
    slug: str             # country slug, e.g. "ukraine"
    name: str             # display name, e.g. "Ukraine"
    region: str | None    # region id, e.g. "europe"
    heat: float           # 0–100 country war PMI (100 = max escalation)
    n_markets: int        # contributing markets
    exchanges: list[str] = []  # distinct venues backing this country's PMI
    # Roster enrichment (D4; None/[] on pre-field cache rows):
    volume_24h: float | None = None       # summed real-money 24h book
    thin: bool = False                    # < $10K/24h → THIN badge
    delta_24h: float | None = None        # heat change vs ~24h ago
    delta_7d: float | None = None         # …vs ~7d ago
    top_contracts: list[WarTopContract] = []  # 3 deepest by 24h volume


class WarByCountryPayload(BaseModel):
    as_of: datetime
    # Keyed by country slug; countries with no scored market are absent.
    countries: dict[str, WarCountryRow]
    world_heat: float | None
    n_countries: int
    n_regions: int
    n_markets: int                    # DISTINCT contributing markets
    # Σ per-country n_markets — a two-sided market counts once per matched
    # country, so this exceeds n_markets; None for pre-2026-07 cache rows.
    n_attributions: int | None = None
    exchanges: list[str] = []
    # World roster aggregates (D4/D5; None on pre-field cache rows):
    world_volume_24h: float | None = None       # world 24h book (hero stat)
    venue_volume: dict[str, float] = {}         # venue → 24h volume (liquidity split)
    concentration: WarConcentration | None = None
    world_delta_24h: float | None = None
    world_delta_7d: float | None = None
    breadth_7d: WarBreadth | None = None
    # When the contract prices in this payload were read (the 10-min snapshot);
    # the heats themselves are the hourly score's. None on older cache rows.
    prices_as_of: datetime | None = None


class WarByCountryEnvelope(BaseModel):
    summary: str
    data: WarByCountryPayload


class WarContractRow(BaseModel):
    market_id: int
    title: str
    venue: str
    yes_pct: float          # the market's own latest YES probability, 0–100
    direction: int          # +1 escalation / -1 peace
    score: float            # directed score, 0–1 (price or 1-price)
    relevancy: float        # 0–1 war-factor relevancy
    volume_24h: float | None
    slug: str | None
    url: str | None = None   # canonical venue deep-link (contract drill-down)
    play_money: bool | None = None  # site credits, not dollars; weight is 0
    # Its share of the country fold (relevancy × liquidity weight); 0 = listed
    # but not scored this tick (untraded / no-data / play-money). None pre-field.
    weight: float | None = None


class WarQuestionMember(BaseModel):
    # One contract folded under an anchor question (the expandable variants).
    market_id: int
    title: str | None = None
    venue: str | None = None
    yes_pct: float | None = None
    volume_24h: float | None = None
    url: str | None = None
    country_slug: str | None = None
    country_name: str | None = None
    play_money: bool | None = None


class WarQuestionRow(BaseModel):
    # One entry in the "By Question" feed. It is a distinct war *question*: the
    # highest-relevancy contract is the facing row, and when several contracts
    # are the same question (dated/reworded variants across venues) they fold in
    # as ``members`` with ``n_contracts`` / ``venues`` summarising the anchor.
    market_id: int
    title: str | None = None
    venue: str | None = None
    yes_pct: float | None = None
    direction: int | None = None
    relevancy: float | None = None
    volume_24h: float | None = None
    url: str | None = None
    country_slug: str | None = None
    country_name: str | None = None
    country_heat: float | None = None
    # Anchor fold (absent/singleton for a lone contract or a pre-anchor cache row):
    anchor_id: int | None = None
    n_contracts: int = 1
    venues: list[str] = []
    members: list[WarQuestionMember] = []
    play_money: bool | None = None
    # When ``title`` is the fold's LLM-written headline, the facing contract's
    # own wording — the exact question the price answers. None when they match.
    facing_title: str | None = None


class WarQuestionsPayload(BaseModel):
    cycle_year: int
    questions: list[WarQuestionRow]
    total: int | None = None          # pre-cap unique contract count
    truncated: bool = False           # True when the snapshot cap bit
    prices_as_of: datetime | None = None


class WarQuestionsEnvelope(BaseModel):
    summary: str
    data: WarQuestionsPayload


class WarCountryDetailPayload(BaseModel):
    slug: str
    name: str
    region: str | None
    heat: float
    n_markets: int
    contracts: list[WarContractRow]
    # How many listed contracts actually scored (weight > 0) and which leaf
    # weighting produced the heat ("liquidity" | "relevancy_only"); None on
    # pre-2026-10-12 cache rows.
    n_scoring: int | None = None
    weighting: str | None = None
    prices_as_of: datetime | None = None


class WarCountryDetailEnvelope(BaseModel):
    summary: str
    data: WarCountryDetailPayload


class WarRegionRow(BaseModel):
    region: str           # region id, e.g. "europe"
    name: str             # display name, e.g. "Europe"
    heat: float           # 0–100 region war PMI
    n_entities: int       # countries in the region's roster
    n_scored: int         # countries that scored this tick
    weight: float         # region's GDP/pop weight in the world fold
    exchanges: list[str] = []  # distinct venues backing this region's PMI
    volume_24h: float | None = None  # summed real-money 24h book (None on pre-field cache)
    delta_24h: float | None = None   # heat change vs ~24h ago (D4)
    delta_7d: float | None = None    # …vs ~7d ago


class WarByRegionPayload(BaseModel):
    as_of: datetime
    regions: dict[str, WarRegionRow]
    world_heat: float | None
    n_regions: int
    n_countries: int
    world_volume_24h: float | None = None
    world_delta_24h: float | None = None
    world_delta_7d: float | None = None


class WarByRegionEnvelope(BaseModel):
    summary: str
    data: WarByRegionPayload


class WarRegionDetailPayload(BaseModel):
    region: str
    name: str
    heat: float
    n_entities: int
    n_scored: int
    n_markets: int = 0                # DISTINCT contributing markets in the region
    n_attributions: int | None = None  # Σ per-country counts (multi-country markets repeat)
    n_holdings: int = 0               # distinct contracts (unique URLs)
    exchanges: list[str] = []         # distinct venues across the region
    countries: list[WarCountryRow]   # the scored countries in this region


class WarRegionDetailEnvelope(BaseModel):
    summary: str
    data: WarRegionDetailPayload


class WarSummaryCitation(BaseModel):
    # A cited contract in the daily note — real core_markets URL, role labels
    # the computed reason it was picked (deepest book / highest reading / …).
    market_id: int | None = None
    title: str | None = None
    venue: str | None = None
    yes_pct: float | None = None
    volume_24h: float | None = None
    url: str | None = None
    role: str


class WarSummaryPayload(BaseModel):
    # The deterministic daily war note (D6): template-from-numbers paragraphs
    # rebuilt 00:10Z from the enriched war snapshot — derived, never authored.
    generated_at: str
    paragraphs: list[str]
    citations: list[WarSummaryCitation] = []


class WarSummaryEnvelope(BaseModel):
    summary: str
    data: WarSummaryPayload


class WarTrendPoint(BaseModel):
    date: str               # ISO date, e.g. "2026-06-25"
    value: float            # heat 0–100 that day
    # Methodology (index-def) version behind the day's snapshot — def bumps
    # move the level, so charts can distinguish those from market moves.
    # None for cache rows predating the version field (2026-07).
    version: int | None = None


class WarCountryTrendsPayload(BaseModel):
    slug: str
    days: int
    points: list[WarTrendPoint]


class WarCountryTrendsEnvelope(BaseModel):
    summary: str
    data: WarCountryTrendsPayload


class WarQuestionTrendsPayload(BaseModel):
    # A question anchor's facing-market price over time (its "Market %" line),
    # so the question page can show a trend chart like the country page.
    market_id: int
    days: int
    points: list[WarTrendPoint]


class WarQuestionTrendsEnvelope(BaseModel):
    summary: str
    data: WarQuestionTrendsPayload


class WarRegionTrendsPayload(BaseModel):
    region: str
    days: int
    points: list[WarTrendPoint]


class WarRegionTrendsEnvelope(BaseModel):
    summary: str
    data: WarRegionTrendsPayload


class WarWorldTrendsPayload(BaseModel):
    days: int
    points: list[WarTrendPoint]


class WarWorldTrendsEnvelope(BaseModel):
    summary: str
    data: WarWorldTrendsPayload


class WarLastUpdatedPayload(BaseModel):
    generated_at: datetime | None


class WarLastUpdatedEnvelope(BaseModel):
    summary: str
    data: WarLastUpdatedPayload


class MarketsDriver(BaseModel):
    """One driver tile on the markets board — a moneyness/kind bucket of the
    component set (see pmi_core.engine.markets_board.classify)."""

    key: str
    name: str  # "At-the-money"
    sub: str | None  # "$59.9k–$61k · closes Jun 24–25"
    # 0–100 signed reading of the bucket; doubles as the tile's heat color.
    # None when no member has a price snapshot.
    value: float | None
    delta_14d: float | None  # None until ≥14d of snapshots exist
    weight_pct: float  # Σ relevancy share of the bucket, 0–100
    n_markets: int
    # Time-binned 0–100 series across the snapshot window; None when fewer
    # than 2 bins have data (no mock sparklines).
    spark: list[float | None] | None


class MarketsRow(BaseModel):
    """One row of the PMI holdings list under the board."""

    market_id: int
    title: str
    prob: float | None  # last_price × 100
    temp: float | None  # per-market signed contribution mapped 0–100
    venues: list[str]
    tags: list[str]  # [driver name, venue display]
    bucket: str  # question-type key (above/below/range/monthly_avg/intraday/ath/other)
    anchor: str  # underlying-question key — the BTC analogue of a War country
    anchor_label: str  # human-readable question (strike normalised out)
    contracts: int  # size of its driver bucket (ladder membership)
    volume_24h: float | None
    url: str | None  # NULL throughout the pmi_btc snapshot (pre-0015 data)


class MarketsSeriesPoint(BaseModel):
    """Score history point. Spans ALL definition versions (methodology moved
    v1→v5, so the frontend labels the chart 'all versions' honestly)."""

    as_of: datetime
    score: float | None
    version: int
    component_count: int


class MarketsBoardPayload(BaseModel):
    index_id: str
    version: int
    as_of: datetime
    score: float | None
    regime: str  # "Deep bear" … "Euphoric" (markets_board.regime_for)
    implied_spot: float | None  # from breakdown.moneyness_weighting
    live_contracts: int  # unresolved core_markets in the index's venues
    exchanges: list[str]  # distinct component venues, most-common first
    holdings: int  # driver buckets with ≥1 market
    component_count: int
    drivers: list[MarketsDriver]
    series: list[MarketsSeriesPoint]
    rows: list[MarketsRow]
    # Same-basket 24h repricing: the reading change over only the contracts
    # priced both now and ~24h ago, so a daily strike-ladder roll doesn't read
    # as a move (CORR-1.14). None on pre-2026-10-13 cached rows / thin baskets.
    delta_24h_same_basket: float | None = None
    # Share (0–1) of the current priced basket that had no price 24h ago. High
    # turnover means a raw score-to-score 24h delta is mostly composition.
    basket_turnover_24h: float | None = None


class MarketsBoardEnvelope(BaseModel):
    summary: str
    data: MarketsBoardPayload


class AnchorTrendPayload(BaseModel):
    # A TimeChartModel — the anchor's aggregate Bull/Bear trend, pre-computed into
    # ts_page_cache by markets-board-snapshot (no per-request scan).
    data: list[float | None] = []
    xLabels: list[str] = []
    labels: list[str] = []


class AnchorTrendEnvelope(BaseModel):
    summary: str
    data: AnchorTrendPayload


class MarketSearchHit(BaseModel):
    market_id: int
    title: str
    venue: str
    url: str | None = None
    last_price: float | None = None   # P(YES), 0..1
    volume_24h: float | None = None
    closes_at: str | None = None


class MarketSearchPayload(BaseModel):
    query: str
    total: int          # capped at the scan limit, not a full count
    results: list[MarketSearchHit]


class MarketSearchEnvelope(BaseModel):
    summary: str
    data: MarketSearchPayload


class AppSettingsResponse(BaseModel):
    """Client-facing app settings (legacy war-index /api/settings shape)."""

    future_phrase: str


class PromptRecord(BaseModel):
    """One prompt as the admin editor sees it (legacy /api/admin/prompts shape).

    ``content`` maps to ``core_prompts.template``; ``model`` / ``temperature``
    surface the active production factor model bound to that prompt version
    (display-only — promotion happens via `pmi-core models`). ``top_p`` /
    ``reasoning_effort`` have no platform equivalent yet and stay None.
    """

    content: str
    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    reasoning_effort: str | None = None


class PromptsSaveResponse(BaseModel):
    status: str
    # name → new version number, only for prompts whose content actually changed
    new_versions: dict[str, int]


class WcMatchupSource(BaseModel):
    """One venue's read on P(team_a advances) for a matchup."""

    source: str  # "polymarket:direct" / "kalshi:direct" / "polymarket:ladder:qf" / …
    p_a: float
    volume_24h: float | None
    # Two-leg / complementary books only: raw price sum before normalisation
    # (distance from 1.0 = vig + staleness).
    book_sum: float | None = None


class WcMatchup(BaseModel):
    """One fixture row (all probabilities are P(team_a advances))."""

    round: str  # "R16" / "QF" / "TBD" (wc_matchups.infer_round)
    team_a: str
    team_b: str
    p_a: float
    p_b: float
    odds_a: float | None  # decimal odds; None when the side is priced at 0
    odds_b: float | None
    edge: float  # max cross-source p_a spread (0.0 when single-sourced)
    sweet: bool
    sweet_reasons: list[str]  # "underdog_odds" / "cross_source_edge"
    sources: list[WcMatchupSource]
    # R16 only: team → P(win the QF | advance) read off the ladder ratio.
    lookahead_win_next: dict[str, float] | None = None


class WcTeamLadder(BaseModel):
    """A team's advance-probability ladder (None = rung not listed/priced)."""

    qf: float | None
    sf: float | None
    win: float | None
    sf_gemini: float | None


class WcParams(BaseModel):
    sweet_odds: float
    min_edge: float


class WcMatchupsPayload(BaseModel):
    generated_at: datetime
    matchups: list[WcMatchup]
    teams: dict[str, WcTeamLadder]
    params: WcParams


class WcMatchupsEnvelope(BaseModel):
    summary: str
    data: WcMatchupsPayload


class EsportsMatchSource(BaseModel):
    """One venue's read on the match — P(team_a wins), for the cross-venue edge."""

    venue: str
    p_a: float | None  # 0–100
    volume_24h: float
    # Pre-vig-strip sum of the two YES legs (1.043 = 4.3% vig book); None when
    # the venue lists one two-outcome market or a leg's book is empty (D14).
    book_sum: float | None = None


class EsportsMatch(BaseModel):
    """One cross-venue-folded match. ``prob_*`` are the volume-weighted, vig-
    stripped 0–100 win %; ``edge`` is the cross-venue spread in P(team_a)."""

    key: str
    slug: str  # readable id, e.g. "cs2-noir-verse-vs-ex-rustec-aug18"
    game: str  # "cs2" | "valorant" | "lol"
    team_a: str
    team_b: str
    team_a_abbr: str  # short code (Kalshi ticker) or the full name if none
    team_b_abbr: str
    prob_a: float | None
    prob_b: float | None
    favorite: str | None
    edge: float  # cross-venue disagreement in P(team_a), 0–100 (0 = single venue/agree)
    tournament: str | None
    starts_at: str | None  # ISO 8601, None when the venue omits a close time
    volume_24h: float
    n_contracts: int
    venues: list[str]
    market_ids: list[int]  # backing markets, for the match-detail holdings
    sources: list[EsportsMatchSource]


class EsportsSettledMatch(EsportsMatch):
    """A recently settled match (D14): the fold's final read plus the winner
    where a resolved YES leg (or a pinned price) states it — never guessed."""

    settled: bool = True
    winner: str | None = None
    settled_at: str | None = None


class EsportsMatchesPayload(BaseModel):
    generated_at: datetime
    game: str | None  # the game filter applied (None = all v1 games)
    matches: list[EsportsMatch]
    n: int
    # Matches settled within ~72h, newest first (empty on pre-field caches).
    settled: list[EsportsSettledMatch] = []


class EsportsMatchesEnvelope(BaseModel):
    summary: str
    data: EsportsMatchesPayload


class EsportsHolding(BaseModel):
    """One backing contract of a match, for the detail page's holdings list."""

    market_id: int
    title: str
    venue: str
    price: float | None  # P(YES), 0..1
    volume_24h: float | None
    url: str | None
    category: str  # "Match Winner" | "Map N" | "Total Maps" | "Handicap" | "Other"
    # The team a YES pays on, where the venue states it (kalshi yes_sub_title /
    # robinhood title grammar); None elsewhere — never guessed (D14).
    side: str | None = None


class EsportsMatchDetailPayload(BaseModel):
    generated_at: datetime
    match: EsportsMatch
    holdings: list[EsportsHolding]


class EsportsMatchDetailEnvelope(BaseModel):
    summary: str
    data: EsportsMatchDetailPayload


class EsportsTrendPoint(BaseModel):
    date: str  # ISO 8601, hourly bucket
    value: float | None  # P(favourite wins), 0–100


class EsportsTrendPayload(BaseModel):
    generated_at: datetime
    favourite: str | None
    days: int
    points: list[EsportsTrendPoint]


class EsportsTrendEnvelope(BaseModel):
    summary: str
    data: EsportsTrendPayload


class EsportsVolumePoint(BaseModel):
    date: str            # ISO 8601, hourly bucket
    value: float | None  # the match's summed 24h book at that hour


class EsportsVolumePayload(BaseModel):
    # The match's 24h book sampled hourly from the esports-match cache history
    # (cadence-safe — reads the cache key's own history, no ts_price_snapshots
    # scan). It is a liquidity-activity sparkbar, not per-hour traded volume.
    generated_at: datetime
    days: int
    points: list[EsportsVolumePoint]
    change_pct: float | None = None  # % change first→last over the window


class EsportsVolumeEnvelope(BaseModel):
    summary: str
    data: EsportsVolumePayload


# resolve forward ref
ScoreEnvelope.model_rebuild()
HistoryEnvelope.model_rebuild()
