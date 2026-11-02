"""W7 — /war route handler unit tests (no DB).

Calls the route coroutines directly with a fake session whose cache reads return
a war_compose-shaped payload, so the reshape + envelope logic is exercised on
the host. The DB-backed end-to-end variant (mirroring test_routes_maga.py) runs
under the Docker e2e harness.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime

import pytest

from pmi_api.routes import war

# A national payload exactly as war_compose.compose_national emits it.
NATIONAL = {
    "computed_at": "2026-06-25T00:00:00+00:00",
    "cycle_year": 2026,
    "world_heat": 62.5,
    "n_countries": 2,
    "n_regions": 2,
    "n_markets": 3,
    "exchanges": ["polymarket"],
    "countries": {
        "ukraine": {"slug": "ukraine", "name": "Ukraine", "region": "europe", "heat": 80.0, "n_markets": 2},
        "taiwan": {"slug": "taiwan", "name": "Taiwan", "region": "asia", "heat": 60.0, "n_markets": 1},
    },
    "regions": {
        "europe": {"region": "europe", "name": "Europe", "heat": 80.0, "n_entities": 52, "n_scored": 1, "weight": 0.4},
        "asia": {"region": "asia", "name": "Asia", "heat": 60.0, "n_entities": 30, "n_scored": 1, "weight": 0.6},
    },
}

COUNTRY = {
    "slug": "ukraine", "name": "Ukraine", "region": "europe", "heat": 80.0, "n_markets": 2,
    "cycle_year": 2026,
    "contracts": [
        {"market_id": 1, "title": "Will there be war in Ukraine?", "venue": "polymarket",
         "yes_pct": 80.0, "direction": 1, "score": 0.8, "relevancy": 0.9, "volume_24h": 100.0, "slug": "s1"},
    ],
}


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Returns a preset scalar (cache payload) and preset execute rows."""

    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    async def scalar(self, _stmt):
        return self._scalar

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        # History reads run inside a savepoint (pmi_api.db_guard.bounded).
        yield


@pytest.mark.asyncio
async def test_by_country_envelope():
    r = await war.war_by_country(as_of=None, cycle=2026, session=_FakeSession(scalar=NATIONAL))
    assert r.data.world_heat == 62.5
    assert set(r.data.countries) == {"ukraine", "taiwan"}
    assert r.data.countries["ukraine"].name == "Ukraine"
    assert r.data.n_markets == 3
    assert r.data.exchanges == ["polymarket"]


@pytest.mark.asyncio
async def test_by_country_503_when_cold():
    with pytest.raises(war.HTTPException) as ei:
        await war.war_by_country(as_of=None, cycle=2026, session=_FakeSession(scalar=None))
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_country_detail_carries_contracts():
    r = await war.war_country_detail(slug="ukraine", cycle=2026, session=_FakeSession(scalar=COUNTRY))
    assert r.data.heat == 80.0
    assert len(r.data.contracts) == 1
    assert r.data.contracts[0].direction == 1
    assert r.data.contracts[0].venue == "polymarket"


@pytest.mark.asyncio
async def test_by_region_envelope():
    r = await war.war_by_region(as_of=None, cycle=2026, session=_FakeSession(scalar=NATIONAL))
    assert set(r.data.regions) == {"europe", "asia"}
    assert r.data.regions["europe"].heat == 80.0
    assert r.data.regions["asia"].weight == 0.6


@pytest.mark.asyncio
async def test_region_detail_lists_scored_countries_sorted():
    r = await war.war_region_detail(region_id="europe", cycle=2026, session=_FakeSession(scalar=NATIONAL))
    assert r.data.name == "Europe"
    assert [c.slug for c in r.data.countries] == ["ukraine"]  # only europe country


@pytest.mark.asyncio
async def test_region_detail_404_unknown():
    with pytest.raises(war.HTTPException) as ei:
        await war.war_region_detail(region_id="atlantis", cycle=2026, session=_FakeSession(scalar=NATIONAL))
    assert ei.value.status_code == 404


# A slim_start far in the past, so trend_series treats the slim series as
# covering the window and maps its rows without the full-key fallback.
_SLIM_COVERS = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_world_trends_maps_slim_trend_rows():
    """The hourly fold (one point per bucket, last snapshot wins) happens in SQL
    inside page_cache.trend_series (DISTINCT ON over the slim trend key) — the
    DB-backed e2e covers the real read. Here the fake returns the SQL row shape
    (date, text value, text version) and we assert the route's mapping."""
    t = datetime(2026, 6, 25, tzinfo=UTC)
    rows = [
        (date(2026, 6, 24), "55.0", None),   # pre-version cache row
        (date(2026, 6, 25), "60.0", "12"),
    ]
    session = _FakeSession(rows=rows, scalar=_SLIM_COVERS)
    r = await war.war_world_trends(days=14, as_of=t, cycle=2026, session=session)
    assert [(p.date, p.value, p.version) for p in r.data.points] == [
        ("2026-06-24", 55.0, None),
        ("2026-06-25", 60.0, 12),
    ]


@pytest.mark.asyncio
async def test_country_trends_maps_slim_trend_rows():
    t = datetime(2026, 6, 25, tzinfo=UTC)
    rows = [(date(2026, 6, 25), "80.0", "12")]
    session = _FakeSession(rows=rows, scalar=_SLIM_COVERS)
    r = await war.war_country_trends(slug="Ukraine", days=14, as_of=t, cycle=2026, session=session)
    assert r.data.slug == "ukraine"
    assert [(p.date, p.value) for p in r.data.points] == [("2026-06-25", 80.0)]


@pytest.mark.asyncio
async def test_questions_summary_reports_cap():
    payload = {
        "cycle_year": 2026,
        "questions": [{"market_id": 1}, {"market_id": 2}],
        "total": 523,
        "truncated": True,
    }
    r = await war.war_questions(cycle=2026, session=_FakeSession(scalar=payload))
    assert r.data.total == 523
    assert r.data.truncated is True
    assert r.summary == "Top 2 of 523 war contracts across all countries."


@pytest.mark.asyncio
async def test_questions_backfills_total_for_old_cache_rows():
    payload = {"cycle_year": 2026, "questions": [{"market_id": 1}]}
    r = await war.war_questions(cycle=2026, session=_FakeSession(scalar=payload))
    assert r.data.total == 1
    assert r.data.truncated is False
    assert r.summary == "1 war contracts across all countries."


# --------------------------------------------------------------------------
# 2026-10-12 — the daily note's citations are re-priced at read time
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_citations_are_repriced_live():
    cached = {
        "paragraphs": ["The world reading stands at 16.0."],
        "citations": [
            {"market_id": 38351, "title": "Will the U.S. invade Iran before 2027?", "venue": "polymarket",
             "yes_pct": 16.0, "volume_24h": 10.0, "url": "u", "role": "deepest book"},
            {"market_id": 999, "title": "gone", "venue": "kalshi", "yes_pct": 5.0, "volume_24h": None,
             "url": None, "role": "highest reading"},
        ],
    }
    session = _FakeSession(scalar=cached, rows=[(38351, 0.2)])
    out = await war._with_live_citations(session, cached)
    assert out["citations"][0]["yes_pct"] == 20.0      # live price
    assert out["citations"][1]["yes_pct"] == 5.0       # unknown id keeps the note's figure
    assert cached["citations"][0]["yes_pct"] == 16.0   # cache payload untouched
    assert out["paragraphs"] == cached["paragraphs"]


@pytest.mark.asyncio
async def test_summary_citations_without_ids_pass_through():
    cached = {"paragraphs": [], "citations": []}
    assert await war._with_live_citations(_FakeSession(scalar=cached), cached) is cached
