"""Gold-set file format + the taxonomy registry.

Storage is a pair of files per version, living in git beside the prompts they
are bound to:

    pmi_core/eval/gold/<taxonomy>-v<N>.jsonl        one GoldItem per line
    pmi_core/eval/gold/<taxonomy>-v<N>.meta.json    strata populations

The split exists because the metrics need something the item rows cannot carry:
how big each stratum was in the POPULATION. Sampling is deliberately
non-uniform (see ``sampling``), so a raw count over the sample is not an
estimate of anything — every rate has to be re-weighted by
``population / sampled`` per stratum. Losing the sidecar makes the JSONL
uninterpretable, which is why it is a sibling file rather than a derived value.

Two conventions carried over from the prompt/index-def contract:

* **``rubric_sha256`` binds a label to a definition.** A gold label is only
  meaningful relative to the rubric that produced it, exactly as an
  ``audit_evaluations`` row is only meaningful relative to its
  ``prompt_sha256``. Editing a factor prompt invalidates the labels made under
  it — the same "bump, don't edit in place" rule as ``ensure_prompt``.
* **Version bumps, never in-place edits.** Re-labelling with a better protocol
  is v2, so a metric regression can always be attributed to either the system
  or the yardstick, never ambiguously to both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GOLD_DIR = Path(__file__).resolve().parent / "gold"

# Sampling strata. The names are load-bearing: `metrics` re-weights by them and
# `sampling` fills them, so adding one means teaching both.
#
# `near_miss` is the stratum that justifies the whole apparatus. The candidate
# strata are drawn from markets the selector ALREADY returns, so on their own
# they can only measure precision. False negatives exist exclusively outside
# that pool and are invisible to any sample scoped to it.
#
# The three sampled-from-the-corpus strata are a PARTITION — selected /
# adjacent-but-unselected / residual — because `metrics` re-weights each item by
# population/sampled, and overlapping strata would count the same markets twice
# at two different weights.
Stratum = Literal[
    "candidate_positive",
    "candidate_zero",
    "near_miss",
    "model_disagreement",
    "corpus_random",
]

STRATA: tuple[Stratum, ...] = (
    "candidate_positive",
    "candidate_zero",
    "near_miss",
    "model_disagreement",
    "corpus_random",
)

STRATUM_RATIONALE: dict[Stratum, str] = {
    "candidate_positive": "selector returned it AND the live factors scored it in — measures precision on what currently counts",
    "candidate_zero": "selector returned it but the factors zeroed it — the 73 % of spend that buys a rejection",
    "near_miss": "semantically adjacent but NOT selected — the only window onto false negatives, including ones cut by the semantic top_k rather than by the cosine floor",
    "model_disagreement": "two production models disagreed on it — highest information per label; empty for a factor only ever run under one model",
    "corpus_random": "uniform draw from the residual (neither selected nor adjacent) — anchors the prevalence estimate, and disjoint from the others so the weights partition",
}


class GoldSpec(BaseModel):
    """What a taxonomy's gold set is *about*.

    ``questions`` is the full set of factor ids labelled per market, and it is
    deliberately wider than ``membership_question``. One labelling call can
    answer several factors for the same market at no extra cost (the same
    observation that motivates bundling the production factor calls), and the
    extra fields make the set immediately reusable for direction / magnitude
    regressions instead of membership only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    taxonomy: str
    version: int
    questions: tuple[str, ...]
    membership_question: str
    # Which implementation answers the harness's five questions — see
    # `taxonomy.resolve_taxonomy`. 'index_def' reads a YAML def and cached LLM
    # judgments; 'gdp_board' runs the National Economy regex chain.
    kind: Literal["index_def", "gdp_board"] = "index_def"
    # Required for kind='index_def'; None for boards that have no YAML def.
    index_id: str | None = None
    # Venue scope for the corpus_random / near_miss strata. None = the index
    # def's own `venues` (which may itself be None = unrestricted).
    venues: tuple[str, ...] | None = None
    # Adjacency axis. Empty = take it from the def's `type: semantic` selectors,
    # which is right for an index def and impossible for a board — a taxonomy
    # with no anchor at all gets no `near_miss` stratum and therefore no view of
    # its own false negatives.
    anchors: tuple[str, ...] = ()
    # question → prompt_ref, for taxonomies whose questions are not YAML factors.
    prompt_refs: dict[str, str] = Field(default_factory=dict)

    @property
    def stem(self) -> str:
        return f"{self.taxonomy}-v{self.version}"

    @model_validator(mode="after")
    def _coherent(self) -> GoldSpec:
        if self.membership_question not in self.questions:
            raise ValueError(
                f"{self.taxonomy}: membership_question "
                f"'{self.membership_question}' is not in questions"
            )
        if self.kind == "index_def" and not self.index_id:
            raise ValueError(f"{self.taxonomy}: kind='index_def' needs an index_id")
        if self.kind != "index_def":
            missing = [q for q in self.questions if q not in self.prompt_refs]
            if missing:
                raise ValueError(
                    f"{self.taxonomy}: kind='{self.kind}' has no YAML factors to read "
                    f"rubrics from, so every question needs a prompt_ref (missing: {missing})"
                )
        return self


GOLD_SPECS: dict[str, GoldSpec] = {
    # First taxonomy. BTC is the right place to start on a model-only protocol:
    # "does this market resolve on BTC's spot price" is close to objective
    # (the rubric in prompts/factors/btc_price_relevance-v1.md enumerates the
    # negative classes), so a labeller's answer is checkable against title
    # structure rather than against taste — unlike "is this geopolitical
    # instability", where the label IS the product decision.
    #
    # It is also where the waste is most concentrated: btc_price_relevance
    # fires on 94.9 % of what the selector hands it, at ~8.4 s per call on the
    # T4 (measured over 11,231 live evaluations).
    "btc-membership": GoldSpec(
        taxonomy="btc-membership",
        version=1,
        index_id="btc-bull-bear-index",
        questions=("btc_price_relevance", "btc_major_move", "btc_direction"),
        membership_question="btc_price_relevance",
    ),
    # Same shape as BTC — YAML def, semantic anchor, three LLM factors — which
    # is exactly why it is the second one. If the harness only ever ran on the
    # taxonomy it was written against, "it generalises" would be an assertion.
    #
    # The interesting difference is the corpus: kalshi lists dense strike ladders
    # for the S&P, so `min_volume_24h: 1` is doing much more work here than on
    # BTC, and the candidate_zero stratum should be full of untraded strikes
    # rather than off-topic markets.
    "sp500-membership": GoldSpec(
        taxonomy="sp500-membership",
        version=1,
        index_id="sp500-bull-bear-index",
        questions=("sp500_price_relevance", "sp500_major_move", "sp500_direction"),
        membership_question="sp500_price_relevance",
    ),
    # The National Economy board — no YAML def, no factors, no audit rows.
    # Membership is four regexes deep in engine/gdp_board.py, one of which
    # blacklists the literal string "nate silver". Nothing has ever measured it.
    #
    # `anchors` has to be supplied here because there is no `type: semantic`
    # selector to read one from, and it is written to describe the CONCEPT
    # rather than the title shape — "the economy shrinks" has to land near the
    # anchor even though the board's own prefilter would never see it, or the
    # near_miss stratum just re-finds markets the prefilter already caught.
    "gdp-membership": GoldSpec(
        taxonomy="gdp-membership",
        version=1,
        kind="gdp_board",
        questions=("gdp_us_growth_membership",),
        membership_question="gdp_us_growth_membership",
        anchors=(
            "United States real GDP growth for a calendar quarter or year — how "
            "fast the US economy expands or contracts, whether output grows above "
            "or below a given percentage rate, and whether the economy shrinks.",
        ),
        prompt_refs={
            "gdp_us_growth_membership": "prompts/factors/gdp_us_growth_membership-v1"
        },
    ),
}


class Vote(BaseModel):
    """One labeller's answer for one question on one market.

    Kept individually rather than folded into a consensus so the gold file can
    be re-adjudicated under a different rule (e.g. "2-of-3 instead of
    unanimous") without re-spending the labelling budget, and so the contested
    rate stays auditable.
    """

    model_config = ConfigDict(extra="forbid")

    source: str  # model id, or "structural" for the deterministic vote
    value: float | None  # None = abstained (structural rule had no opinion)
    confidence: float | None = None
    rationale: str | None = None


class GoldItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: int
    venue: str
    title: str
    description: str | None = None
    stratum: Stratum

    # Consensus per question. Absent when every labeller for it disagreed.
    labels: dict[str, float] = Field(default_factory=dict)
    votes: dict[str, list[Vote]] = Field(default_factory=dict)
    # 'labelled'   — unanimous on the membership question, usable as truth
    # 'contested'  — labellers split; reported, never used as a pass/fail gate
    # 'unlabelled' — sampled but not yet run through the labeller
    status: Literal["labelled", "contested", "unlabelled"] = "unlabelled"

    # The definition these labels are relative to: {question: prompt sha256}.
    rubric_sha256: dict[str, str] = Field(default_factory=dict)
    labelled_at: datetime | None = None

    # Frozen at sampling time so metrics can weight an error by how much the
    # market could actually move the index. Re-reading it later would drift.
    liquidity: float | None = None
    is_play_money: bool = False

    def membership(self, spec: GoldSpec) -> float | None:
        return self.labels.get(spec.membership_question)


class StratumMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    population: int
    sampled: int
    rationale: str = ""

    @property
    def weight(self) -> float:
        """Population units represented by each sampled item (Horvitz-Thompson).

        A stratum sampled to exhaustion weighs 1.0; the corpus_random stratum,
        drawn from millions, weighs enormously — which is correct and is why an
        unweighted precision over this file would be meaningless.
        """
        if self.sampled <= 0:
            return 0.0
        return self.population / self.sampled


class GoldSetMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taxonomy: str
    version: int
    # None for board taxonomies, which have no YAML def to name.
    index_id: str | None = None
    kind: str = "index_def"
    created_at: datetime
    # Free-text note on how the sample was drawn / anything a reader needs to
    # know before trusting the numbers.
    note: str = ""
    strata: dict[str, StratumMeta] = Field(default_factory=dict)

    def weights(self) -> dict[str, float]:
        return {name: s.weight for name, s in self.strata.items()}


def gold_paths(spec: GoldSpec, base_dir: Path | None = None) -> tuple[Path, Path]:
    d = base_dir or GOLD_DIR
    return d / f"{spec.stem}.jsonl", d / f"{spec.stem}.meta.json"


def save_gold(
    spec: GoldSpec,
    meta: GoldSetMeta,
    items: list[GoldItem],
    base_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write both files, items sorted by market_id.

    Sorting is not cosmetic: these files live in git and get re-written by
    ``eval label``, so a stable order keeps the diff to the labels that
    actually changed instead of a whole-file churn.
    """
    jsonl_path, meta_path = gold_paths(spec, base_dir)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        it.model_dump_json(exclude_none=False)
        for it in sorted(items, key=lambda i: i.market_id)
    ]
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta_path.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return jsonl_path, meta_path


def load_gold(
    spec: GoldSpec, base_dir: Path | None = None
) -> tuple[GoldSetMeta, list[GoldItem]]:
    jsonl_path, meta_path = gold_paths(spec, base_dir)
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"no gold set at {jsonl_path} — run `pmi-core eval sample {spec.taxonomy}` first"
        )
    meta = GoldSetMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    items = [
        GoldItem.model_validate_json(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return meta, items


def new_meta(spec: GoldSpec, note: str = "") -> GoldSetMeta:
    return GoldSetMeta(
        taxonomy=spec.taxonomy,
        version=spec.version,
        index_id=spec.index_id,
        kind=spec.kind,
        created_at=datetime.now(UTC),
        note=note,
    )


def resolve_spec(taxonomy: str) -> GoldSpec:
    try:
        return GOLD_SPECS[taxonomy]
    except KeyError:
        known = ", ".join(sorted(GOLD_SPECS)) or "(none registered)"
        raise KeyError(f"unknown taxonomy '{taxonomy}'. Known: {known}") from None


__all__ = [
    "GOLD_DIR",
    "GOLD_SPECS",
    "STRATA",
    "STRATUM_RATIONALE",
    "GoldItem",
    "GoldSetMeta",
    "GoldSpec",
    "Stratum",
    "StratumMeta",
    "Vote",
    "gold_paths",
    "load_gold",
    "new_meta",
    "resolve_spec",
    "save_gold",
]
