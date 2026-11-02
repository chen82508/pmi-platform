"""Platform-wide slim-trend keys / projection (pmi_core.engine.trend_store)."""

from __future__ import annotations

from datetime import datetime

from pmi_core.engine import trend_store as ts


def test_slim_key_suffixes_the_family_token():
    assert ts.slim_key("war:2026") == "war-trend:2026"
    assert ts.slim_key("maga:2026") == "maga-trend:2026"  # matches the existing key
    assert ts.slim_key("senate-board:us-x") == "senate-board-trend:us-x"  # ditto
    assert ts.slim_key("gdp-board:us-gdp") == "gdp-board-trend:us-gdp"
    assert ts.slim_key("climate-hub") == "climate-hub-trend"
    assert ts.slim_key("climate-question:gw__a") == "climate-question-trend:gw__a"


def test_family_and_has_trend():
    assert ts.family("war:2026") == "war"
    assert ts.family("climate-hub") == "climate-hub"
    assert ts.has_trend("war:2026")
    assert not ts.has_trend("war-country:iraq:2026")  # a latest-only key


def test_covers_literal_and_wildcard_paths():
    assert ts.covers("war:2026", ("countries", "iraq", "heat"))
    assert ts.covers("war:2026", ("world_heat",))
    assert ts.covers("maga:2026", ("states", "TX", "office_heat", "senate"))
    assert ts.covers("senate-board:x", ("prob_by_state", "TX"))
    assert not ts.covers("gdp-board:x", ("foo",))
    assert not ts.covers("war:2026", ("contracts",))


def test_project_keeps_only_scalars_and_per_entity_subtrees():
    war = {
        "version": 21,
        "world_heat": 11.6,
        "world_volume_24h": 500,
        "extra": "drop",
        "countries": {"iraq": {"heat": 81, "volume_24h": 9, "contracts": [1, 2], "title": "x"}},
        "regions": {"me": {"heat": 40, "volume_24h": 1, "junk": 9}},
    }
    assert ts.project("war:2026", war) == {
        "version": 21,
        "world_heat": 11.6,
        "world_volume_24h": 500,
        "countries": {"iraq": {"heat": 81, "volume_24h": 9}},
        "regions": {"me": {"heat": 40, "volume_24h": 1}},
    }


def test_project_handles_nested_maga_offices_and_districts():
    maga = {
        "national_heat": 46,
        "chambers": {"senate": {"pmi": 50, "junk": 1}},
        "states": {
            "TX": {"heat": 60, "office_heat": {"senate": 62}, "district_heat": {"01": 55}, "contracts": [1]}
        },
        "groups": ["drop"],
    }
    assert ts.project("maga:2026", maga) == {
        "national_heat": 46,
        "chambers": {"senate": {"pmi": 50}},
        "states": {"TX": {"heat": 60, "office_heat": {"senate": 62}, "district_heat": {"01": 55}}},
    }


def test_project_omits_fields_a_payload_does_not_have():
    assert ts.project("gdp-board:x", {"score": 48.4, "n_anchors": 6}) == {"score": 48.4}


def test_slim_values_none_for_untracked_family():
    assert ts.slim_values("war-country:iraq:2026", {}, datetime(2026, 1, 1)) is None


def test_slim_values_builds_the_insert_row():
    at = datetime(2026, 1, 1)
    assert ts.slim_values("gdp-board:x", {"score": 1.0, "big": [1, 2, 3]}, at) == {
        "cache_key": "gdp-board-trend:x",
        "payload": {"score": 1.0},
        "computed_at": at,
    }


def test_source_families_are_the_spec_keys_not_slim_keys():
    fams = ts.source_families()
    assert "war" in fams and "senate-board" in fams
    # slim families are never sources (they are not spec keys)
    assert "war-trend" not in fams and "senate-board-trend" not in fams


def test_covers_rejects_non_leaf_parent_paths():
    # a reader must ask for a scalar leaf, not a kept parent object
    assert not ts.covers("maga:2026", ("states",))
    assert not ts.covers("maga:2026", ())
