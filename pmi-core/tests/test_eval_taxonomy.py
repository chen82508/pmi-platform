"""The taxonomy layer: two very different systems answering the same questions.

The point of the abstraction is that ``metrics`` cannot tell an LLM pipeline
from a regex chain, so these tests are mostly about the GDP board — the
implementation that motivated the interface and the one with no LLM anywhere
in it.

Nothing here touches a database. ``candidate_split`` and ``predictions`` need
one, and are exercised on the live box instead; what is checked here is the
pure logic that decides membership and the spec validation that stops a
half-declared taxonomy from reaching the sampler.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmi_core.engine import gdp_board as gb
from pmi_core.eval.schema import GOLD_SPECS, GoldSpec, resolve_spec
from pmi_core.eval.structural import STRUCTURAL_VOTERS
from pmi_core.eval.taxonomy import (
    GdpBoardTaxonomy,
    IndexDefTaxonomy,
    resolve_taxonomy,
)

# ── spec validation ────────────────────────────────────────────────────────


def test_registered_specs_resolve_to_a_taxonomy() -> None:
    for name in GOLD_SPECS:
        tax = resolve_taxonomy(resolve_spec(name))
        assert tax.spec.taxonomy == name


def test_index_def_spec_needs_an_index_id() -> None:
    with pytest.raises(ValidationError, match="needs an index_id"):
        GoldSpec(
            taxonomy="broken",
            version=1,
            questions=("q",),
            membership_question="q",
        )


def test_board_spec_needs_a_prompt_ref_per_question() -> None:
    """A board has no YAML factors, so a missing rubric can only surface here."""
    with pytest.raises(ValidationError, match="prompt_ref"):
        GoldSpec(
            taxonomy="broken",
            version=1,
            kind="gdp_board",
            questions=("q",),
            membership_question="q",
        )


def test_membership_question_must_be_one_of_the_questions() -> None:
    with pytest.raises(ValidationError, match="not in questions"):
        GoldSpec(
            taxonomy="broken",
            version=1,
            index_id="btc-bull-bear-index",
            questions=("a",),
            membership_question="b",
        )


# ── GDP board taxonomy ─────────────────────────────────────────────────────


@pytest.fixture
def gdp() -> GdpBoardTaxonomy:
    tax = resolve_taxonomy(resolve_spec("gdp-membership"))
    assert isinstance(tax, GdpBoardTaxonomy)
    return tax


@pytest.mark.parametrize(
    "title",
    [
        "Will US GDP growth in 2026 be between 2.0% and 2.5%?",
        "GDP growth in 2026? - 1.6% to 2.0%",
        "Will US real GDP growth for Q2 2026 exceed 3%?",
    ],
)
def test_clean_us_growth_markets_are_members(gdp: GdpBoardTaxonomy, title: str) -> None:
    assert gdp._is_member(title) is True


@pytest.mark.parametrize(
    "title",
    [
        # rejected by the title prefilter (no "gdp")
        "Will the US economy enter a recession in 2026?",
        # rejected by _OTHER
        "Will China's 2026 annual GDP growth be 9% or higher?",
        # rejected by _JUNK
        "Who wins the presidency in 2028 and will US real GDP growth exceed 3%?",
        # no single period
        "Will there be negative US GDP growth during Q1, Q2, or Q3 2026?",
        # prefilter rejects the ratio markets outright
        "Will US debt-to-GDP growth exceed 5% in 2026?",
    ],
)
def test_non_members(gdp: GdpBoardTaxonomy, title: str) -> None:
    assert gdp._is_member(title) is False


def test_anchor_comes_from_the_spec_not_a_semantic_selector(gdp: GdpBoardTaxonomy) -> None:
    """A board has no def to read an anchor off, and without one there is no
    near_miss stratum and therefore no view of its own false negatives."""
    anchors = gdp.anchors()
    assert len(anchors) == 1
    assert "GDP" in anchors[0].text
    assert anchors[0].declared_min_similarity is None


def test_universe_excludes_policy_but_not_the_gdp_title_filter(gdp: GdpBoardTaxonomy) -> None:
    """The title prefilter is the first link of the chain being GRADED.

    Folding it into the universe would make every stratum title-scoped, and a
    GDP market whose title never says "GDP" could then never appear in the gold
    set at all — the exact false negative worth finding.
    """
    rendered = " ".join(str(c) for c in gdp.universe_filters()).lower()
    assert "resolved_at" in rendered
    assert "gdp" not in rendered


def test_factor_is_synthesised_from_the_spec(gdp: GdpBoardTaxonomy) -> None:
    factor = gdp.factor_for("gdp_us_growth_membership")
    assert factor.type == "binary"
    assert factor.prompt_ref.endswith("gdp_us_growth_membership-v1")
    # No relevancy aggregation exists on this board, so a weight would be a lie.
    assert factor.weight is None


def test_unknown_question_names_the_spec_not_a_missing_yaml(gdp: GdpBoardTaxonomy) -> None:
    with pytest.raises(KeyError, match="no prompt_ref"):
        gdp.factor_for("sp500_direction")


def test_gdp_has_no_structural_voter() -> None:
    """Deliberate: the obvious deterministic rule IS the system under test.

    If someone adds one later, this test should fail loudly and they should
    have to argue that the new rule is independent of gdp_board's regexes.
    """
    assert "gdp_us_growth_membership" not in STRUCTURAL_VOTERS


# ── the shared title prefilter ─────────────────────────────────────────────


def test_python_prefilter_matches_the_terms_the_sql_uses() -> None:
    """Both are built from the same tuples; this pins the semantics.

    The route, the snapshot job and the harness each build ILIKE clauses from
    these, so a change to the tuples changes all three at once — which is the
    entire reason they moved out of the three hand-copied WHERE clauses.
    """
    assert gb.title_prefilter("Will US GDP growth in 2026 exceed 2%?")
    assert not gb.title_prefilter("Will US inflation exceed 2% in 2026?")  # no 'gdp'
    assert not gb.title_prefilter("US GDP in 2026?")  # no 'growth' / 'real gdp'
    assert not gb.title_prefilter("Will nominal GDP growth exceed 4%?")
    assert not gb.title_prefilter("Will US debt-to-GDP growth exceed 5%?")
    assert gb.title_prefilter("US real GDP for Q1 2026?")  # 'real gdp' satisfies ANY


# ── index-def taxonomy ─────────────────────────────────────────────────────


def test_sp500_reads_its_anchor_and_threshold_off_the_yaml() -> None:
    tax = resolve_taxonomy(resolve_spec("sp500-membership"))
    assert isinstance(tax, IndexDefTaxonomy)
    anchors = tax.anchors()
    assert len(anchors) == 1
    # Carried so the sampler can log it beside the floor it derives from the
    # corpus; the two disagreeing is a finding, not noise.
    assert anchors[0].declared_min_similarity == 0.6


def test_sp500_universe_keeps_the_volume_gate_and_drops_exclude_terms() -> None:
    """`min_volume_24h` is policy (untraded strikes cannot matter);
    `exclude_terms` is a classification rule and must stay gradable."""
    tax = resolve_taxonomy(resolve_spec("sp500-membership"))
    rendered = " ".join(str(c) for c in tax.universe_filters()).lower()
    assert "last_volume_24h" in rendered
    assert "absolute percentage change" not in rendered


def test_index_def_factor_lookup_names_the_def_on_a_miss() -> None:
    tax = resolve_taxonomy(resolve_spec("sp500-membership"))
    with pytest.raises(KeyError, match="sp500-bull-bear-index"):
        tax.factor_for("btc_direction")
