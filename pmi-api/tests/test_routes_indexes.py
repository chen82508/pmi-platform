"""Route tests for /indexes — list, get, score, history, explain.

These tests guard the CORR-3.3 fix (/explain dict-update bug + last_price
join) and lock in the public response shapes consumed by pmi-web + the
future pmi-mcp Tier A tools.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_indexes_returns_current_versions(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes")
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["id"] for r in rows}
    assert ids == {
        "polymarket-war-index",
        "empty-index",
        "us-senate-2026-republican-seats",
        # Null-score fixture seeded for the Micah PR #316 backport tests
        # (test_routes_null_score.py). Listed because is_current=True.
        "null-score-index",
    }
    war = next(r for r in rows if r["id"] == "polymarket-war-index")
    assert war["version"] == 1
    assert war["title"] == "Polymarket War Index"
    assert war["is_current"] is True
    assert len(war["yaml_sha256"]) == 64


@pytest.mark.asyncio
async def test_get_index_404_for_missing(client: AsyncClient) -> None:
    resp = await client.get("/indexes/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"]["code"] == "INDEX_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_index_returns_summary(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/polymarket-war-index")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "polymarket-war-index"
    assert body["version"] == 1


@pytest.mark.asyncio
async def test_get_score_returns_latest(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/polymarket-war-index/score")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["index_id"] == "polymarket-war-index"
    assert data["score"] == pytest.approx(seeded_data["expected_score"])
    assert data["component_count"] == 1
    # Provenance read off the score's own lineage, so a definition with no
    # `venues:` (the default — every real-money venue) still reports what it was
    # built from. The landing's "N contracts · N venues" foot line comes from
    # here; `market_count` is distinct markets even though the lineage holds one
    # row per (market, factor), and it's the only contract count that survives a
    # formula whose component_count means seats or countries.
    assert data["market_count"] == 1
    assert data["venues"] == ["polymarket"]
    # `summary` is the human-readable envelope — should embed the score.
    assert "55.00" in body["summary"]


@pytest.mark.asyncio
async def test_score_provenance_empty_without_lineage(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """The older score row has no component_evaluation_ids: no venues, no
    invented count, and no 500 from an empty IN () list."""
    as_of = seeded_data["older_score_as_of"] + timedelta(hours=1)
    resp = await client.get(
        "/indexes/polymarket-war-index/score", params={"as_of": as_of.isoformat()}
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {**resp.json()["data"], "market_count": 0, "venues": []}


@pytest.mark.asyncio
async def test_get_score_as_of_returns_older(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """`as_of` filter should pick the older score, not the most recent one."""
    as_of = seeded_data["older_score_as_of"] + timedelta(hours=1)
    resp = await client.get(
        "/indexes/polymarket-war-index/score",
        params={"as_of": as_of.isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["score"] == pytest.approx(seeded_data["older_expected_score"])


@pytest.mark.asyncio
async def test_get_score_404_when_no_score_yet(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """`empty-index` has a definition but no ts_index_scores row."""
    resp = await client.get("/indexes/empty-index/score")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NO_SCORE_YET"


@pytest.mark.asyncio
async def test_get_history_returns_ordered_points(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/polymarket-war-index/score/history")
    assert resp.status_code == 200
    points = resp.json()["data"]["points"]
    assert len(points) == 2
    # Ascending order by as_of.
    assert points[0]["score"] == pytest.approx(seeded_data["older_expected_score"])
    assert points[1]["score"] == pytest.approx(seeded_data["expected_score"])


@pytest.mark.asyncio
async def test_get_history_respects_limit_and_window(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """from= / to= window + limit clip the result."""
    from_dt = seeded_data["latest_score_as_of"] - timedelta(minutes=1)
    resp = await client.get(
        "/indexes/polymarket-war-index/score/history",
        params={"from": from_dt.isoformat(), "limit": 5},
    )
    assert resp.status_code == 200
    points = resp.json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["score"] == pytest.approx(seeded_data["expected_score"])


@pytest.mark.asyncio
async def test_get_history_limit_returns_newest_not_oldest(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """`limit` must clip to the NEWEST rows, not the oldest. An index scored more
    than `limit` times in the window (btc/sp500 tick ~40x/day) otherwise freezes
    its sparkline/delta at the n-th oldest row while /score stays fresh."""
    resp = await client.get(
        "/indexes/polymarket-war-index/score/history", params={"limit": 1}
    )
    assert resp.status_code == 200
    points = resp.json()["data"]["points"]
    assert len(points) == 1
    # The single returned point is the LATEST reading, not the older one.
    assert points[0]["score"] == pytest.approx(seeded_data["expected_score"])


@pytest.mark.asyncio
async def test_history_404_for_unknown_index(client: AsyncClient) -> None:
    resp = await client.get("/indexes/nope/score/history")
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# /explain — CORR-3.3 regression tests
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explain_returns_factors_and_relevancy(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """Pre-fix bug: relevancy/direction were hard-coded to 0.0. Now they
    are computed from the loaded AuditEvaluation rows + index def IR."""
    resp = await client.get("/indexes/polymarket-war-index/explain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["index_id"] == "polymarket-war-index"
    assert body["score"] == pytest.approx(seeded_data["expected_score"])
    components = body["components"]
    assert len(components) == 1
    comp = components[0]

    # CORR-3.3 (a): relevancy must reflect Σ(value × weight) / Σ(weight)
    # — single weighted factor with value=1.0 → relevancy=1.0.
    assert comp["relevancy"] == pytest.approx(1.0)
    # `direction` factor with value_numeric=1.0 → direction=+1.
    assert comp["direction"] == pytest.approx(1.0)

    # CORR-3.3 (b): last_price must be sourced from ts_price_snapshots
    # (no longer hard-coded null) — the recent snapshot is 0.60.
    assert comp["last_price"] == pytest.approx(0.60)

    # Factors dict must include both seeded factor_ids.
    assert comp["factors"] == {
        "directly_about_war": pytest.approx(1.0),
        "direction": pytest.approx(1.0),
    }
    assert comp["title"] == "Will the war end this year?"
    assert comp["market_id"] == seeded_data["market_id"]


@pytest.mark.asyncio
async def test_explain_empty_when_score_has_no_components(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    """Older score row has empty component_evaluation_ids → components=[],
    no AuditEvaluation join, no 500 on the array.in_() with an empty list."""
    as_of = seeded_data["older_score_as_of"] + timedelta(hours=1)
    resp = await client.get(
        "/indexes/polymarket-war-index/explain",
        params={"as_of": as_of.isoformat()},
    )
    assert resp.status_code == 200
    assert resp.json()["components"] == []


@pytest.mark.asyncio
async def test_explain_404_when_no_score(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get("/indexes/empty-index/explain")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NO_SCORE_YET"
