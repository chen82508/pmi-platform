"""tag-games negative cache: a "not a sports market" verdict must be PERSISTED.

Before 2026-10-27 `tag_market` returned None for a value=0 verdict and the job
dropped it, so the market re-entered the candidate set and was re-classified by
gpt-4o-mini every hour forever — ~90% of tag-games' OpenAI spend was re-billing
"still not a game". The fix persists a `negative_tag_row` sentinel so the
candidate query (`id NOT IN tagged-at-version`) skips it. These tests pin that:
value=0 → a sentinel row (persisted), malformed extraction → None (retry).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from pmi_core.engine import sports_tag
from pmi_core.engine.sports_tag import (
    _CLASSES,
    _SPORTS,
    CLASSIFIER_VERSION,
    NON_GAME_SENTINEL,
    negative_tag_row,
    tag_market,
)


class _FakeProvider:
    def __init__(self, resp: SimpleNamespace) -> None:
        self._resp = resp

    async def evaluate(self, **_kwargs: object) -> SimpleNamespace:
        return self._resp


def _market(**over: object) -> SimpleNamespace:
    base = {"id": 1, "venue": "polymarket", "title": "A vs B", "closes_at": None,
            "external_id": "ext-1"}
    base.update(over)
    return SimpleNamespace(**base)


def test_negative_row_has_all_not_null_columns() -> None:
    row = negative_tag_row(123)
    assert row["market_id"] == 123
    # NOT-NULL columns on core_market_game_tag must all be populated.
    for col in ("sport", "market_class", "market_type", "resolved_via"):
        assert row[col] == NON_GAME_SENTINEL
    assert row["classifier_version"] == CLASSIFIER_VERSION


def test_negative_row_is_invisible_to_every_board() -> None:
    # Boards filter sport=<real sport> and market_class in game/outright/prop, so
    # the sentinel must match none of those — otherwise it would leak into a board.
    row = negative_tag_row(1)
    assert row["sport"] not in _SPORTS
    assert row["market_class"] not in _CLASSES


async def test_value_zero_is_negative_cached(monkeypatch) -> None:
    resp = SimpleNamespace(value_numeric=0.0, value_label=None, cost_usd=0.0002)
    monkeypatch.setattr(sports_tag, "get_provider", lambda _m: _FakeProvider(resp))
    row, cost = await tag_market(_market(id=42))
    assert row is not None  # persisted, NOT dropped
    assert row["market_id"] == 42
    assert row["sport"] == NON_GAME_SENTINEL
    assert cost == 0.0002


async def test_real_game_returns_a_real_row(monkeypatch) -> None:
    fields = {"market_class": "game", "sport": "mlb", "market_type": "moneyline",
              "home_team": "New York Yankees", "away_team": "Boston Red Sox"}
    resp = SimpleNamespace(value_numeric=1.0, value_label=json.dumps(fields), cost_usd=0.0003)
    monkeypatch.setattr(sports_tag, "get_provider", lambda _m: _FakeProvider(resp))
    row, _ = await tag_market(_market())
    assert row is not None
    assert row["sport"] == "mlb"
    assert row["market_class"] == "game"
    assert row["sport"] != NON_GAME_SENTINEL


async def test_malformed_extraction_retries_not_cached(monkeypatch) -> None:
    # value>=1 but unparseable label is a malformed result, not a verdict: return
    # None so it retries next run (we must not negative-cache a probable game).
    resp = SimpleNamespace(value_numeric=1.0, value_label="{not json", cost_usd=0.0001)
    monkeypatch.setattr(sports_tag, "get_provider", lambda _m: _FakeProvider(resp))
    row, cost = await tag_market(_market())
    assert row is None
    assert cost == 0.0001
