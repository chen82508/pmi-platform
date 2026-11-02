"""Unit tests for tag_races glue.

The classification itself now comes from the aggregator's keyword path
(_extraction_inputs + classify_entities), covered by pmi-core's test_extraction /
test_aggregator and by the tag-vs-keyword reconciliation. Here we pin the only
job-local pure logic: the entity-key → (state, district) reverse mapping the job
uses to shape a persisted row, plus the maga↔seats selector-alignment invariant
the whole tag-path rests on (see test_maga_seats_selectors_aligned).
"""

from __future__ import annotations

from pmi_core.dsl.ir import CategorySelector, KeywordSelector, load_index_def
from pmi_core.engine.pipeline import INDEX_DEFS_DIR

from pmi_workers.jobs.tag_races import _OFFICE_DEFS, _state_district

# The MAGA lean def paired with each office's seats def (the tag source). Kept in
# sync with _OFFICE_DEFS by test_office_defs_cover_every_maga_lean below.
_MAGA_DEFS: dict[str, str] = {
    "senate": "maga-senate-2026",
    "house": "maga-house-2026",
    "governor": "maga-governor-2026",
}


def test_house_key_splits_state_and_district() -> None:
    assert _state_district("us_house_district", "California-20") == ("California", 20)


def test_house_key_hyphenated_state() -> None:
    # rpartition on the LAST hyphen keeps multi-word / hyphenated state names.
    assert _state_district("us_house_district", "New York-3") == ("New York", 3)


def test_house_key_at_large_number() -> None:
    assert _state_district("us_house_district", "South Dakota-1") == ("South Dakota", 1)


def test_house_key_without_district_number() -> None:
    # Defensive: a malformed key without a trailing number keeps the whole key as
    # the state and leaves district None rather than raising.
    assert _state_district("us_house_district", "Wyoming") == ("Wyoming", None)


def test_state_grammar_has_no_district() -> None:
    assert _state_district("us_state", "Ohio") == ("Ohio", None)
    assert _state_district("us_governor", "Texas") == ("Texas", None)


# ── maga ↔ seats selector alignment (the tag-path's load-bearing invariant) ──
# tag-races builds core_market_race_tag by classifying ONLY the seats defs'
# keyword pool (_OFFICE_DEFS). Both the seats indexes AND the MAGA leans then
# select their components from that shared tag set. So the MAGA lean can only see
# a market if the seats def's selector/extraction would have tagged it. If someone
# edits one def's keywords / venues / extraction without the other, the MAGA lean
# silently loses (or mis-keys) contracts with no error. These tests fail loudly on
# that drift.


def _keywords(ir) -> list[str]:
    kw = [s for s in ir.selectors if isinstance(s, KeywordSelector)]
    return sorted([t for s in kw for t in s.terms] + [p for s in kw for p in s.title_patterns])


def _categories(ir) -> list[str]:
    return sorted(s.polymarket_tag for s in ir.selectors if isinstance(s, CategorySelector))


def _extraction_shape(ir) -> tuple | None:
    e = ir.extraction
    return None if e is None else (e.group_by, e.sides, tuple(e.methods), e.cycle)


def test_office_defs_cover_every_maga_lean() -> None:
    # The pairing below must stay exhaustive: every office tag-races classifies
    # has a MAGA lean depending on it, and vice versa.
    assert set(_OFFICE_DEFS) == set(_MAGA_DEFS)


def test_maga_seats_selectors_aligned() -> None:
    for office, seats_id in _OFFICE_DEFS.items():
        maga_ir, _, _ = load_index_def(INDEX_DEFS_DIR / f"{_MAGA_DEFS[office]}.yaml")
        seats_ir, _, _ = load_index_def(INDEX_DEFS_DIR / f"{seats_id}.yaml")

        # Keywords + category + extraction MUST be identical — they define which
        # markets get tagged and under which entity key both indexes then read.
        assert _keywords(maga_ir) == _keywords(seats_ir), (
            f"{office}: maga/seats keyword selectors diverged — tag pool no longer "
            f"matches what the MAGA lean expects"
        )
        assert _categories(maga_ir) == _categories(seats_ir), f"{office}: category selectors diverged"
        assert _extraction_shape(maga_ir) == _extraction_shape(seats_ir), (
            f"{office}: extraction grammar/cycle diverged — entity keys won't match"
        )

        # Venues: the tag pool only covers the seats def's venues, so the MAGA
        # lean's venues must be a subset (else it selects venues that were never
        # tagged). Equal today; subset is the real coverage requirement.
        assert set(maga_ir.venues or []) <= set(seats_ir.venues or []), (
            f"{office}: maga venues {maga_ir.venues} not covered by seats venues "
            f"{seats_ir.venues} — those venues' markets are never tagged"
        )


# ── tag kind: an LLM-named candidate leg is a candidate tag (2026-10-12) ──────

from pmi_core.engine.seat_mapping import EntityObservation  # noqa: E402

from pmi_workers.jobs.tag_races import CLASSIFIER_VERSION, _tag_kind  # noqa: E402


def test_tag_kind_follows_the_consumed_tier() -> None:
    assert _tag_kind("patterns") == "party_direct"
    assert _tag_kind("llm_fallback") == "party_direct"
    assert _tag_kind("candidates") == "candidate"


def test_llm_candidate_observation_keeps_provenance() -> None:
    o = EntityObservation(
        market_id=1, key="California-11", code="CA-11", party="D", price=0.23,
        tier="candidates", venue_rank=0, label="Connie Chan", resolved_via="llm_fallback",
    )
    assert _tag_kind(o.tier) == "candidate"
    assert (o.resolved_via or o.tier) == "llm_fallback"


def test_classifier_version_bumped_for_the_v3_grammar() -> None:
    assert CLASSIFIER_VERSION >= 3
