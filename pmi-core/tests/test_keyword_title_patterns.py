"""KeywordSelector.title_patterns — raw-regex selector terms for formats a
literal keyword can't capture (IBKR's bare "MI-13 General Election" House titles).
"""

from __future__ import annotations

from pathlib import Path

import pmi_core
from pmi_core.dsl.ir import KeywordSelector, load_index_def

_DEFS = Path(pmi_core.__file__).resolve().parent / "index_defs"


def test_title_patterns_defaults_empty():
    s = KeywordSelector(type="keyword", terms=["House"])
    assert s.title_patterns == []


def test_title_patterns_accepted_verbatim():
    pat = r"[A-Z]{2}-[0-9]{1,2} General Election"
    s = KeywordSelector(type="keyword", terms=["House"], title_patterns=[pat])
    assert s.title_patterns == [pat]


def test_house_defs_carry_the_district_pattern():
    # Both the seats index and the MAGA lean must carry it (guard test keeps
    # them aligned); without it IBKR's "MI-13 General Election" legs never select.
    for name in ("us-house-2026-republican-seats", "maga-house-2026"):
        ir, _, _ = load_index_def(_DEFS / f"{name}.yaml")
        pats = [p for s in ir.selectors if isinstance(s, KeywordSelector) for p in s.title_patterns]
        assert any("General Election" in p for p in pats), f"{name} missing district title_pattern"
