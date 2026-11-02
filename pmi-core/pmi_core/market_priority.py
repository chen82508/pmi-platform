"""Market ingest priority: which markets each poller should refresh first.

Tiers (lower = more urgent):

* ``WATCHED`` (P0): a component of the latest score of a current index def, or
  registered by a non-activity watcher (e.g. a board snapshot).
* ``ACTIVE`` (P0, capped): traded in the last 24h, reported by pollers as an
  ``activity:<venue>`` watch.
* ``NEW`` (P1): listed in the last ``new_listing_hours``, measured with the
  ``core_market_id_watermark`` PK clock.

Anything not in ``core_market_priority`` is P2. Every query here touches
``core_markets`` by primary key only: the table has no usable index on its hot
columns (0017), so a filtered scan is a 17 GB seq scan. See
docs/reports/priority_ingestion_blueprint.md §3–§4.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any, NamedTuple, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core.models import CoreMarketPriority, CoreMarketWatch


class Tier(IntEnum):
    WATCHED = 0
    ACTIVE = 1
    NEW = 2


ACTIVITY_WATCHER_PREFIX = "activity:"
BOARD_WATCHER_PREFIX = "board:"

# Used when no watermark is old enough yet: "id > max bigint" selects nothing,
# so a fresh deployment has no P1 tier instead of treating every market as new.
_NO_WATERMARK = 2**63 - 1


class PriorityMarket(NamedTuple):
    market_id: int
    external_id: str


@dataclass(frozen=True)
class RefreshResult:
    by_tier: dict[int, int] = field(default_factory=dict)
    removed: int = 0
    watches_expired: int = 0
    watermark_used: int | None = None
    watermark_taken: int = 0

    @property
    def total(self) -> int:
        return sum(self.by_tier.values())


def activity_watcher(venue: str) -> str:
    return f"{ACTIVITY_WATCHER_PREFIX}{venue}"


async def priority_ids(
    session: AsyncSession, venue: str, max_tier: Tier | int, limit: int
) -> list[PriorityMarket]:
    """In-use markets for ``venue``, most urgent tier first, best rank first.

    Returns ``[]`` when the table is empty (job not deployed yet), so callers
    fall back to their pre-priority behaviour.
    """
    if limit <= 0:
        return []
    stmt = (
        select(CoreMarketPriority.market_id, CoreMarketPriority.external_id)
        .where(CoreMarketPriority.venue == venue, CoreMarketPriority.tier <= int(max_tier))
        .order_by(
            CoreMarketPriority.tier,
            CoreMarketPriority.rank.desc().nulls_last(),
            CoreMarketPriority.market_id,
        )
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [PriorityMarket(r.market_id, r.external_id) for r in rows]


def watch_rows(
    watcher: str, market_ids: Iterable[int], expires_at: datetime
) -> list[dict[str, object]]:
    """Deduplicated insert rows: one statement can't ON CONFLICT the same key twice."""
    return [
        {"market_id": mid, "watcher": watcher, "expires_at": expires_at}
        for mid in sorted(set(market_ids))
    ]


async def register_watch(
    session: AsyncSession,
    watcher: str,
    market_ids: Iterable[int],
    *,
    ttl: timedelta,
    now: datetime,
) -> int:
    """Upsert ``(market_id, watcher)`` watches expiring at ``now + ttl``; returns rows sent."""
    rows = watch_rows(watcher, market_ids, now + ttl)
    if not rows:
        return 0
    stmt = pg_insert(CoreMarketWatch).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CoreMarketWatch.market_id, CoreMarketWatch.watcher],
        set_={"expires_at": stmt.excluded.expires_at},
    )
    await session.execute(stmt)
    return len(rows)


def market_ids_in(payload: object) -> set[int]:
    """Every market_id a payload references — scalar ``market_id`` + ``market_ids``
    arrays, at any depth. ``type(x) is int`` (not ``isinstance``) excludes bools."""
    out: set[int] = set()
    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            mid = node.get("market_id")
            if type(mid) is int:
                out.add(mid)
            mids = node.get("market_ids")
            if isinstance(mids, list):
                out.update(x for x in mids if type(x) is int)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


async def register_board_watch(
    session: AsyncSession,
    name: str,
    market_ids: set[int],
    *,
    now: datetime,
    log: Any,
    ttl: timedelta = timedelta(days=3),
) -> int:
    """Stamp the markets a board snapshot rendered as a ``board:<name>`` watch so
    the priority cron folds them to P0.

    This is the declaration a browse/computed vertical needs: it has no score, so
    its markets never appear in the cron's component lane. Runs in a SAVEPOINT so a
    watch failure rolls back only its own nested transaction (and is logged) — it
    can never poison the outer transaction and sink the board cache the snapshot
    exists to write. Returns the count stamped (0 on empty / failure). ``ttl`` must
    exceed the snapshot cadence; it is re-stamped each run, so a watch only lapses
    if the snapshot itself stops.
    """
    if not market_ids:
        return 0
    try:
        async with session.begin_nested():
            return await register_watch(
                session, f"{BOARD_WATCHER_PREFIX}{name}", market_ids, ttl=ttl, now=now
            )
    except Exception:
        log.warning("board_watch.failed", watcher=name, exc_info=True)
        return 0


# ── refresh ──────────────────────────────────────────────────────────────────

_WATERMARK_AT_OR_BEFORE = text(
    "SELECT max_id FROM core_market_id_watermark "
    "WHERE taken_at <= CAST(:cutoff AS timestamptz) ORDER BY taken_at DESC LIMIT 1"
)
# Until 48h of samples exist, "new" means "listed since we started sampling".
_OLDEST_WATERMARK = text(
    "SELECT max_id FROM core_market_id_watermark ORDER BY taken_at ASC LIMIT 1"
)

# One statement: gather signals, join core_markets by PK for venue/external_id
# and liveness, collapse per market to its most urgent tier, upsert.
_UPSERT_PRIORITY = text(
    f"""
WITH latest AS (
    SELECT DISTINCT ON (s.index_definition_id) s.component_evaluation_ids AS eval_ids
    FROM ts_index_scores s
    JOIN core_index_definitions d ON d.id = s.index_definition_id
    WHERE d.is_current
    ORDER BY s.index_definition_id, s.as_of DESC
),
components AS (
    SELECT DISTINCT e.market_id, 'component'::text AS reason, {int(Tier.WATCHED)} AS tier
    FROM latest
    CROSS JOIN LATERAL unnest(latest.eval_ids) AS u(eval_id)
    JOIN audit_evaluations e ON e.id = u.eval_id
),
watches AS (
    SELECT w.market_id, w.watcher AS reason,
           CASE WHEN position('{ACTIVITY_WATCHER_PREFIX}' in w.watcher) = 1
                THEN {int(Tier.ACTIVE)} ELSE {int(Tier.WATCHED)} END AS tier
    FROM core_market_watch w
    WHERE w.expires_at > CAST(:now AS timestamptz)
),
fresh AS (
    SELECT m.id AS market_id, 'new_listing'::text AS reason, {int(Tier.NEW)} AS tier
    FROM core_markets m
    WHERE m.id > CAST(:watermark AS bigint)
),
signals AS (
    SELECT market_id, reason, tier FROM components
    UNION ALL SELECT market_id, reason, tier FROM watches
    UNION ALL SELECT market_id, reason, tier FROM fresh
)
INSERT INTO core_market_priority
    (market_id, venue, external_id, tier, rank, reasons, refreshed_at)
SELECT m.id, m.venue, m.external_id, min(s.tier), CAST(m.last_volume_24h AS real),
       array_agg(DISTINCT s.reason ORDER BY s.reason), CAST(:now AS timestamptz)
FROM signals s
JOIN core_markets m ON m.id = s.market_id
WHERE m.resolved_at IS NULL
  AND (m.closes_at IS NULL OR m.closes_at > CAST(:now AS timestamptz))
GROUP BY m.id
ON CONFLICT (market_id) DO UPDATE SET
    venue = EXCLUDED.venue,
    external_id = EXCLUDED.external_id,
    tier = EXCLUDED.tier,
    rank = EXCLUDED.rank,
    reasons = EXCLUDED.reasons,
    refreshed_at = EXCLUDED.refreshed_at
RETURNING tier
"""
)

_DELETE_DEMOTED = text(
    "DELETE FROM core_market_priority WHERE refreshed_at < CAST(:now AS timestamptz)"
)
_DELETE_EXPIRED_WATCHES = text(
    "DELETE FROM core_market_watch WHERE expires_at <= CAST(:now AS timestamptz)"
)
_TAKE_WATERMARK = text(
    "INSERT INTO core_market_id_watermark (taken_at, max_id) "
    "SELECT CAST(:now AS timestamptz), coalesce(max(id), 0) FROM core_markets "
    "ON CONFLICT (taken_at) DO NOTHING RETURNING max_id"
)
_PRUNE_WATERMARKS = text(
    "DELETE FROM core_market_id_watermark WHERE taken_at < CAST(:cutoff AS timestamptz)"
)


def count_tiers(tiers: Iterable[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for t in tiers:
        out[int(t)] = out.get(int(t), 0) + 1
    return out


async def _rowcount(session: AsyncSession, stmt: Any, params: dict[str, Any]) -> int:
    # DML results are CursorResults at runtime; execute() is typed as plain Result.
    result = cast(CursorResult[Any], await session.execute(stmt, params))
    return int(result.rowcount or 0)


async def _watermark_for(session: AsyncSession, cutoff: datetime) -> int | None:
    found = (
        await session.execute(_WATERMARK_AT_OR_BEFORE, {"cutoff": cutoff})
    ).scalar_one_or_none()
    if found is not None:
        return int(found)
    oldest = (await session.execute(_OLDEST_WATERMARK)).scalar_one_or_none()
    return int(oldest) if oldest is not None else None


async def refresh_market_priority(
    session: AsyncSession,
    *,
    now: datetime,
    new_listing_hours: float = 48.0,
    watermark_retention_days: float = 7.0,
    statement_timeout_ms: int = 30_000,
) -> RefreshResult:
    """Rebuild ``core_market_priority`` in the caller's transaction.

    Readers see the old or the new set, never an empty one: upsert first, then
    delete the rows this run didn't refresh.
    """
    await session.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
    await session.execute(text("SET LOCAL lock_timeout = 2000"))

    watermark = await _watermark_for(session, now - timedelta(hours=new_listing_hours))
    tiers = (
        (
            await session.execute(
                _UPSERT_PRIORITY,
                {"now": now, "watermark": watermark if watermark is not None else _NO_WATERMARK},
            )
        )
        .scalars()
        .all()
    )
    removed = await _rowcount(session, _DELETE_DEMOTED, {"now": now})
    expired = await _rowcount(session, _DELETE_EXPIRED_WATCHES, {"now": now})
    taken = (await session.execute(_TAKE_WATERMARK, {"now": now})).scalar_one_or_none()
    await session.execute(
        _PRUNE_WATERMARKS, {"cutoff": now - timedelta(days=watermark_retention_days)}
    )

    return RefreshResult(
        by_tier=count_tiers(tiers),
        removed=removed,
        watches_expired=expired,
        watermark_used=watermark,
        watermark_taken=int(taken or 0),
    )
