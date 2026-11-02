"""home-snapshot job — pre-compute the homepage's landing data into ts_page_cache.

The homepage ("/") assembles ~12 endpoints (per-index score + history, seat /
markets / gdp boards, movers, stats). Instead of re-running that fan-out on every
visit (force-dynamic, and the worst-hit surface when the data-plane box is busy),
this runs it ONCE every ~10 min and stashes the result under `landing-home`, so
the route serves from a single cached read — the same cron-into-cache pattern every
/micah vertical uses.

The assembly logic lives in pmi-web (TypeScript); this worker just triggers it via
pmi-web's internal `/api/home-data` and stores the JSON, so there's no second
implementation of the landing logic to drift.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import structlog
from pmi_core.db import session_scope
from pmi_core.models import TsPageCache

from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.home_snapshot")

# pmi-web (prod build) on the compose network; override per environment.
_WEB_URL = os.environ.get("PMI_WEB_INTERNAL_URL", "http://pmi-web-prod:3000")


@register("home-snapshot", queue="snapshot")
async def run() -> dict:
    computed_at = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.get(f"{_WEB_URL}/api/home-data")
        resp.raise_for_status()
        payload = resp.json()

    cards = payload.get("cards") or []
    if not cards:
        # A degraded fan-out (all cards dropped) must not overwrite a good cache —
        # the route would rather serve slightly stale real data than an empty page.
        log.warning("home_snapshot.empty", keys=sorted(payload.keys()))
        return {"written": False, "cards": 0}

    async with session_scope() as session:
        session.add(
            TsPageCache(cache_key="landing-home", payload=payload, computed_at=computed_at)
        )
        await session.commit()

    n = {"cards": len(cards), "movers": len(payload.get("movers") or [])}
    log.info("home_snapshot.done", **n)
    return {"written": True, **n}
