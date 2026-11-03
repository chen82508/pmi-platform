"""Route tests for null ts_index_scores.score.

The aggregator now persists ``score=None`` when the pipeline can't produce
a meaningful score this tick (below ``min_components`` / zero relevancy).
These tests pin the API behaviour for that case across ``/score``,
``/score/history`` and ``/explain``:

  * /score returns 200 with ``data.score = null`` and a "n/a" in the
    summary string (no 500 from ``float(None)``).
  * /score/history surfaces ``score: null`` in the affected point and a
    real float in the next-older point, so the frontend can render gaps.
  * /explain returns ``score: null`` and an empty ``components`` list
    when the null score has no lineage IDs (the typical
    below-min_components shape).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_score_route_returns_null_for_null_latest(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{seeded_data['null_index_id']}/score")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["data"]["score"] is None
    assert payload["data"]["component_count"] == 0
    # The summary string must degrade gracefully — no "55.00"-style number
    # and no exception. We just assert "n/a" appears so the contract is
    # explicit.
    assert "n/a" in payload["summary"]


async def test_history_route_mixes_null_and_real_scores(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{seeded_data['null_index_id']}/score/history")
    assert resp.status_code == 200
    points = resp.json()["data"]["points"]
    # 2 rows seeded: the older real one and the latest null one. They come
    # back in ascending as_of order per the route.
    assert len(points) == 2
    assert points[0]["score"] == pytest.approx(seeded_data["null_older_expected_score"])
    assert points[1]["score"] is None


async def test_explain_route_returns_null_score_and_empty_components(
    client: AsyncClient, seeded_data: dict[str, Any]
) -> None:
    resp = await client.get(f"/indexes/{seeded_data['null_index_id']}/explain")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["score"] is None
    assert payload["components"] == []
