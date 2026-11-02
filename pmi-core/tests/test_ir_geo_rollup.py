"""W3 — IR wiring for geo_weighted_mean / region rollup / gdp_pop_weighted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmi_core.dsl.ir import IndexDef

_FACTORS = [
    {"id": "direction", "type": "ternary", "prompt_ref": "p/direction-v1", "weight": None},
    {"id": "about_war", "type": "binary", "prompt_ref": "p/about-war-v1", "weight": 100},
    {"id": "country_x", "type": "extraction", "prompt_ref": "p/country-extraction-v1", "weight": None},
]


def _def(**agg) -> dict:
    aggregation = {
        "formula": "geo_weighted_mean",
        "min_components": 1,
        "rollup": {
            "cross_entity": "gdp_pop_weighted",
            "group_through": "region",
            "include_zero_groups": True,
        },
    }
    aggregation.update(agg)
    return {
        "id": "war-test",
        "version": 1,
        "title": "War Test",
        "selectors": [{"type": "keyword", "terms": ["war"]}],
        "factors": _FACTORS,
        "extraction": {"group_by": "country", "sides": "none", "methods": ["patterns", "llm_fallback"]},
        "aggregation": aggregation,
    }


def test_valid_geo_def():
    d = IndexDef.model_validate(_def())
    assert d.aggregation.formula == "geo_weighted_mean"
    assert d.aggregation.rollup.cross_entity == "gdp_pop_weighted"
    assert d.aggregation.rollup.group_through == "region"
    assert d.aggregation.rollup.include_zero_groups is True


def test_geo_requires_country_group_by():
    bad = _def()
    bad["extraction"]["group_by"] = "us_state"
    bad["extraction"]["sides"] = "party"
    with pytest.raises(ValidationError, match="geo_weighted_mean"):
        IndexDef.model_validate(bad)


def test_region_rollup_requires_country():
    bad = _def()
    bad["aggregation"]["formula"] = "entity_lean_mean"
    bad["extraction"]["group_by"] = "us_house_district"
    bad["extraction"]["sides"] = "party"
    bad["aggregation"]["rollup"] = {"cross_entity": "equal", "group_through": "region"}
    with pytest.raises(ValidationError, match="group_through 'region' needs"):
        IndexDef.model_validate(bad)


def test_gdp_pop_weighted_only_geo_formula():
    bad = _def()
    bad["aggregation"]["formula"] = "entity_lean_mean"
    bad["extraction"]["group_by"] = "country"
    with pytest.raises(ValidationError):
        IndexDef.model_validate(bad)


def test_include_zero_groups_only_geo_formula():
    bad = _def()
    bad["aggregation"]["formula"] = "entity_lean_mean"
    bad["aggregation"]["rollup"] = {
        "cross_entity": "equal",
        "include_zero_groups": True,
    }
    bad["extraction"]["group_by"] = "us_state"
    bad["extraction"]["sides"] = "party"
    with pytest.raises(ValidationError, match="include_zero_groups"):
        IndexDef.model_validate(bad)


def test_geo_without_extraction_rejected():
    bad = _def()
    del bad["extraction"]
    with pytest.raises(ValidationError, match="requires an extraction block"):
        IndexDef.model_validate(bad)
