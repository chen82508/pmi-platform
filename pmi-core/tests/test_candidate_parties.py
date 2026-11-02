"""Curated candidate → party overrides (config/candidate_parties.yaml)."""

from __future__ import annotations

from pmi_core.engine.candidate_parties import normalize_name, override_party


def test_normalize_name_strips_party_suffix_and_case() -> None:
    assert normalize_name("Jon  Husted (R)") == "jon husted"
    assert normalize_name("Roy Cooper (D-NC)") == "roy cooper"
    assert normalize_name(None) == ""


def test_listed_overrides_resolve() -> None:
    assert override_party("Justin Murphy", "New Jersey") == "R"
    assert override_party("Justin Murphy", "New Jersey", "senate") == "R"
    assert override_party("Todd Achilles", "Idaho") == "I"
    assert override_party("Todd Achilless", "Idaho") == "I"  # kalshi spelling


def test_unlisted_is_none() -> None:
    assert override_party("Jon Husted (R)", "Ohio") is None
    assert override_party("Justin Murphy", "Texas") is None
    assert override_party("Justin Murphy", "New Jersey", "governor") is None
    assert override_party("Connie Chan", "California-11") is None
    assert override_party("", "New Jersey") is None
