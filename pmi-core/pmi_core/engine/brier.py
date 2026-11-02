"""Brier calibration report over resolved markets (T6).

Once the resolution sweeper stamps ``core_markets.(resolved_at, resolution)``
with YES/NO ground truth, every historical ``ts_price_snapshots`` row becomes
a scored prediction. This module answers: *how well calibrated were market
prices at a given lead time before resolution?*

For each resolved YES/NO market, take the LAST snapshot at or before
``resolved_at - lead_hours`` and score it:

    brier = mean over markets of (p - outcome)²

where p is the mid price ((bid+ask)/2) when both quotes exist, else
``last_price``, and outcome is 1 for YES / 0 for NO. Lower is better; an
always-0.5 coin flip scores 0.25, so anything materially under 0.25 means the
prices carried real signal at that lead time.

Why this matters for LLM tuning (the actual T6 goal): the same machinery
extends to *index-level* calibration — score an index's component evaluations
against how their markets resolved, so a Tier 1 model swap or prompt-v2 change
can be judged on ground truth instead of vibes. Index-level join (via
``ts_index_scores.component_audit_ids`` lineage) is the follow-up; venue-level
price calibration here is the skeleton that proves the ground truth flows.

INVALID resolutions are excluded (no binary outcome to score against).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from pmi_core.db import session_scope

_REPORT_SQL = text(
    """
    SELECT
        m.venue,
        count(*)                                   AS n,
        avg(power(s.p - m.y, 2))                   AS brier,
        avg(s.p)                                   AS mean_p,
        avg(m.y)                                   AS base_rate
    FROM (
        SELECT id, venue, resolved_at,
               CASE resolution WHEN 'YES' THEN 1.0 ELSE 0.0 END AS y
        FROM core_markets
        WHERE resolution IN ('YES', 'NO')
          -- CAST: asyncpg cannot infer a NULL bind's type (AmbiguousParameterError)
          AND (CAST(:venue AS TEXT) IS NULL OR venue = CAST(:venue AS TEXT))
    ) m
    JOIN LATERAL (
        SELECT CASE
                   WHEN s.bid IS NOT NULL AND s.ask IS NOT NULL
                       THEN (s.bid + s.ask) / 2.0
                   ELSE s.last_price
               END AS p
        FROM ts_price_snapshots s
        WHERE s.market_id = m.id
          AND s.snapshot_at <= m.resolved_at - make_interval(hours => :lead_hours)
          AND (s.last_price IS NOT NULL OR (s.bid IS NOT NULL AND s.ask IS NOT NULL))
        ORDER BY s.snapshot_at DESC
        LIMIT 1
    ) s ON s.p IS NOT NULL
    GROUP BY m.venue
    ORDER BY n DESC
    """
)


async def brier_report(venue: str | None = None, lead_hours: int = 24) -> dict[str, Any]:
    """Per-venue Brier of market prices `lead_hours` before resolution.

    `base_rate` is the venues' YES frequency — the no-skill reference Brier is
    `base_rate × (1 − base_rate)` (predicting the base rate every time), so
    report both rather than assuming 0.25.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                _REPORT_SQL, {"venue": venue, "lead_hours": lead_hours}
            )
        ).all()

    venues = [
        {
            "venue": r.venue,
            "n": int(r.n),
            "brier": round(float(r.brier), 4),
            "mean_p": round(float(r.mean_p), 4),
            "base_rate": round(float(r.base_rate), 4),
            "no_skill_brier": round(float(r.base_rate) * (1 - float(r.base_rate)), 4),
        }
        for r in rows
    ]
    return {"lead_hours": lead_hours, "venues": venues}
