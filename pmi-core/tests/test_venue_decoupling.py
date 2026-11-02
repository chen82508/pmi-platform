"""Venue is no longer a membership filter (2026-06-14).

Locks in the three behaviours of decoupling venue from membership:

1. **IR**: `venues` is optional and defaults to None (= all venues). An index
   only restricts venue when it declares one explicitly (structural scoping).
2. **Tier 0 cost backstop**: when `embedding_tier0_max_survivors` is set, the
   prefilter keeps only the top-K candidates by cosine, so a flood of
   cross-venue near-floor matches can't blow up the factor LLM loop.
3. **Tier 0 fail-open**: markets with no embedding are always kept and are not
   subject to the cap (can't be ranked) — recall over a stray factor call.
"""

from __future__ import annotations

import pytest

from pmi_core.dsl.ir import (
    FactorSpec,
    IndexDef,
    KeywordSelector,
    SemanticSelector,
)
from pmi_core.engine import pipeline
from pmi_core.models import CoreMarket


def _ir(*, venues=..., semantic: bool = True) -> IndexDef:
    selectors = [KeywordSelector(type="keyword", terms=["war"])]
    if semantic:
        selectors.append(
            SemanticSelector(type="semantic", anchor="armed conflict", min_similarity=0.6)
        )
    kwargs = dict(
        id="venue-test",
        version=1,
        title="Venue Test",
        owner="test",
        selectors=selectors,
        factors=[
            FactorSpec(id="rel", type="binary", prompt_ref="prompts/factors/rel-v1", weight=1.0),
        ],
    )
    if venues is not ...:
        kwargs["venues"] = venues
    return IndexDef(**kwargs)


# ── 1. IR venues optional ──────────────────────────────────────────────────


def test_venues_defaults_to_none() -> None:
    """Omitting `venues:` means no venue restriction — all venues."""
    assert _ir().venues is None


def test_venues_explicit_list_is_honoured() -> None:
    ir = _ir(venues=["forecastex"])
    assert ir.venues == ["forecastex"]


def test_play_money_excluded_from_membership_by_default() -> None:
    """Default keeps play-money out of membership (count would swamp the score)."""
    assert _ir().include_play_money is False


def test_venues_empty_list_rejected() -> None:
    """An explicit list must be non-empty — use None / omit for 'all'."""
    with pytest.raises(ValueError):
        _ir(venues=[])


# ── 1b. min_volume_24h membership gate (BTC v5) ────────────────────────────


def _vol_ir(min_volume_24h):
    return IndexDef(
        id="vol-test", version=1, title="Vol Test", owner="test",
        selectors=[KeywordSelector(type="keyword", terms=["btc"])],
        factors=[FactorSpec(id="rel", type="binary", prompt_ref="prompts/factors/rel-v1", weight=1.0)],
        min_volume_24h=min_volume_24h,
    )


def test_min_volume_defaults_to_none() -> None:
    """Omitting it → no volume gate, so every existing index is unchanged."""
    assert _ir().min_volume_24h is None


def test_min_volume_honoured() -> None:
    """A positive threshold is accepted (selector then requires traded markets)."""
    assert _vol_ir(1.0).min_volume_24h == 1.0


def test_min_volume_rejects_negative() -> None:
    """Volume can't be negative (ge=0.0); 0.0 is allowed but a no-op gate."""
    with pytest.raises(ValueError):
        _vol_ir(-5)


# ── 2 & 3. Tier 0 survivor cap + fail-open ─────────────────────────────────


def _market(mid: int, venue: str = "polymarket") -> CoreMarket:
    return CoreMarket(id=mid, venue=venue, external_id=f"e{mid}", title=f"m{mid}")


class _FakeStore:
    """Returns a preset cosine per market id; missing ids => no embedding."""

    def __init__(self, cosines: dict[int, float]) -> None:
        self._cosines = cosines

    async def cosine_for_markets(self, *, market_ids, query_embedding, model):
        return {mid: self._cosines[mid] for mid in market_ids if mid in self._cosines}


@pytest.fixture
def _patch_tier0(monkeypatch):
    async def fake_embed(text, model=None):
        return [0.0, 0.0, 0.0]

    monkeypatch.setattr(pipeline, "embed_query", fake_embed)

    def install(cosines):
        monkeypatch.setattr(pipeline, "get_vector_store", lambda: _FakeStore(cosines))

    return install


@pytest.mark.asyncio
async def test_tier0_cap_keeps_top_k_by_cosine(_patch_tier0, monkeypatch) -> None:
    """With a cap of 2, only the 2 highest-cosine above-floor markets survive."""
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_min_cosine", 0.5)
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_max_survivors", 2)
    markets = [_market(i) for i in range(1, 6)]
    # All clear the 0.5 floor; cosines ascending with id.
    cosines = {1: 0.55, 2: 0.60, 3: 0.70, 4: 0.80, 5: 0.90}
    _patch_tier0(cosines)

    survivors, stats = await pipeline._tier0_prefilter(markets, _ir())

    assert stats["enabled"] is True
    assert {m.id for m in survivors} == {4, 5}  # top-2 cosine
    assert stats["dropped_by_cap"] == 3
    assert stats["dropped"] == 0


@pytest.mark.asyncio
async def test_tier0_no_cap_keeps_all_above_floor(_patch_tier0, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_min_cosine", 0.5)
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_max_survivors", None)
    markets = [_market(i) for i in range(1, 6)]
    cosines = {1: 0.55, 2: 0.60, 3: 0.70, 4: 0.80, 5: 0.90}
    _patch_tier0(cosines)

    survivors, stats = await pipeline._tier0_prefilter(markets, _ir())

    assert {m.id for m in survivors} == {1, 2, 3, 4, 5}
    assert stats["dropped_by_cap"] == 0


@pytest.mark.asyncio
async def test_tier0_no_embedding_failopen_survives_cap(_patch_tier0, monkeypatch) -> None:
    """Markets with no vector are kept regardless of the cap (can't be ranked)."""
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_min_cosine", 0.5)
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_max_survivors", 1)
    markets = [_market(i) for i in range(1, 5)]
    # ids 1,2 have vectors above floor; ids 3,4 have NO embedding row.
    cosines = {1: 0.80, 2: 0.90}
    _patch_tier0(cosines)

    survivors, stats = await pipeline._tier0_prefilter(markets, _ir())

    ids = {m.id for m in survivors}
    # cap=1 keeps the single best-cosine (id 2) + both fail-open markets (3, 4).
    assert ids == {2, 3, 4}
    assert stats["no_embedding"] == 2
    assert stats["dropped_by_cap"] == 1


@pytest.mark.asyncio
async def test_tier0_noop_without_semantic_anchor(_patch_tier0, monkeypatch) -> None:
    """No semantic anchor → no embedding reference → gate is a no-op (all kept)."""
    monkeypatch.setattr(pipeline.settings, "embedding_tier0_max_survivors", 1)
    markets = [_market(i) for i in range(1, 5)]
    _patch_tier0({})

    survivors, stats = await pipeline._tier0_prefilter(markets, _ir(semantic=False))

    assert len(survivors) == 4
    assert stats["enabled"] is False
