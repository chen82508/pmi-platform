"""`_enrich_kalshi_nfl`: only KXNFLGAME / KXNFLSPREAD may land as a full-game
moneyline or spread. The LLM tagged KXNFLWINMARGIN as a side-less moneyline,
which the NFL board would have folded as P(home wins) (2026-10-29 audit).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pmi_core.engine.sports_tag import _enrich_kalshi_nfl


def _row(market_type: str) -> dict:
    return {"sport": "nfl", "market_class": "game", "market_type": market_type,
            "home_team": "Green Bay Packers", "away_team": None, "game_date": None}


def _market(ext: str) -> SimpleNamespace:
    return SimpleNamespace(venue="kalshi", external_id=ext)


@pytest.mark.parametrize(
    ("ext", "mtype"),
    [("KXNFLGAME-26OCT29CARGB-GB", "moneyline"), ("KXNFLSPREAD-26OCT29CARGB-GB3", "spread")],
)
def test_game_and_spread_tickers_keep_their_type(ext: str, mtype: str) -> None:
    row = _row(mtype)
    _enrich_kalshi_nfl(row, _market(ext))
    assert row["market_type"] == mtype
    assert row["away_team"] == "Carolina Panthers"
    assert row["resolved_via"] == "ticker+llm"


@pytest.mark.parametrize(
    ("ext", "mtype"),
    [("KXNFLWINMARGIN-26OCT29CARGB-GB7", "moneyline"), ("KXNFL1HSPREAD-26OCT29CARGB-GB3", "spread")],
)
def test_other_kalshi_nfl_series_become_props(ext: str, mtype: str) -> None:
    row = _row(mtype)
    _enrich_kalshi_nfl(row, _market(ext))
    assert row["market_type"] == "prop"


def test_non_board_types_untouched() -> None:
    row = _row("total")
    _enrich_kalshi_nfl(row, _market("KXNFLTOTAL-26OCT29CARGB-46"))
    assert row["market_type"] == "total"
