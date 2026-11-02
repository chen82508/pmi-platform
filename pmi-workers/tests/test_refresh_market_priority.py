"""refresh-market-priority against a real (ephemeral) Postgres.

The refresh is one hand-written SQL statement, so the only meaningful test is
running it: tier precedence, liveness filtering, demotion, watch expiry, the
id-watermark clock, and the poller-side read (`priority_ids`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pmi_core.market_priority import (
    Tier,
    activity_watcher,
    priority_ids,
    refresh_market_priority,
    register_watch,
)
from pmi_core.models import (
    AuditEvaluation,
    CoreIndexDefinition,
    CoreMarket,
    CoreMarketIdWatermark,
    CoreMarketPriority,
    CorePrompt,
    TsIndexScore,
)
from sqlalchemy import select, text

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def _clean_priority_tables(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE core_market_priority, core_market_watch, core_market_id_watermark, "
                "ts_index_scores, audit_evaluations, core_prompts, core_index_definitions, "
                "core_markets RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()


async def _market(session, venue: str, ext: str, **kw) -> int:
    m = CoreMarket(venue=venue, external_id=ext, title=f"{venue} {ext}", **kw)
    session.add(m)
    await session.flush()
    return m.id


async def _score_with_components(session, market_ids: list[int], *, current: bool = True) -> None:
    """A current index def whose latest score lists `market_ids` as components."""
    d = CoreIndexDefinition(
        index_id=f"idx-{len(market_ids)}-{current}",
        version=1,
        title="t",
        definition={},
        yaml_source="",
        yaml_sha256="0" * 64,
        is_current=current,
    )
    p = CorePrompt(name="p", version=1, template="t", sha256="1" * 64)
    session.add_all([d, p])
    await session.flush()
    evals = [
        AuditEvaluation(
            market_id=mid,
            index_definition_id=d.id,
            factor_id="f",
            prompt_id=p.id,
            prompt_sha256=p.sha256,
            model_id="stub",
        )
        for mid in market_ids
    ]
    session.add_all(evals)
    await session.flush()
    # An older score with a different component must NOT count — latest only.
    stale = await _market(session, "polymarket", f"stale-{d.id}")
    stale_eval = AuditEvaluation(
        market_id=stale,
        index_definition_id=d.id,
        factor_id="f",
        prompt_id=p.id,
        prompt_sha256=p.sha256,
        model_id="stub",
    )
    session.add(stale_eval)
    await session.flush()
    session.add_all(
        [
            TsIndexScore(
                index_definition_id=d.id,
                as_of=NOW - timedelta(hours=2),
                component_count=1,
                component_evaluation_ids=[stale_eval.id],
            ),
            TsIndexScore(
                index_definition_id=d.id,
                as_of=NOW - timedelta(minutes=5),
                component_count=len(evals),
                component_evaluation_ids=[e.id for e in evals],
            ),
        ]
    )
    await session.flush()


async def _rows(session) -> dict[str, CoreMarketPriority]:
    rows = (await session.execute(select(CoreMarketPriority))).scalars().all()
    return {r.external_id: r for r in rows}


async def test_components_are_watched_and_older_scores_ignored(session_factory) -> None:
    async with session_factory() as s:
        comp = await _market(s, "polymarket", "comp", last_volume_24h=10.0)
        await _score_with_components(s, [comp])
        result = await refresh_market_priority(s, now=NOW)
        rows = await _rows(s)
        await s.commit()

    assert set(rows) == {"comp"}
    assert rows["comp"].tier == Tier.WATCHED
    assert rows["comp"].reasons == ["component"]
    assert result.by_tier == {0: 1}


async def test_non_current_definition_contributes_nothing(session_factory) -> None:
    async with session_factory() as s:
        comp = await _market(s, "polymarket", "old-def")
        await _score_with_components(s, [comp], current=False)
        await refresh_market_priority(s, now=NOW)
        assert await _rows(s) == {}


async def test_most_urgent_tier_wins_and_reasons_merge(session_factory) -> None:
    async with session_factory() as s:
        both = await _market(s, "kalshi", "both")
        active_only = await _market(s, "kalshi", "active")
        await _score_with_components(s, [both])
        await register_watch(
            s, activity_watcher("kalshi"), [both, active_only], ttl=timedelta(hours=24), now=NOW
        )
        await refresh_market_priority(s, now=NOW)
        rows = await _rows(s)

    assert rows["both"].tier == Tier.WATCHED
    assert rows["both"].reasons == ["activity:kalshi", "component"]
    assert rows["active"].tier == Tier.ACTIVE


async def test_board_watch_is_watched_tier(session_factory) -> None:
    async with session_factory() as s:
        m = await _market(s, "gemini", "board")
        await register_watch(s, "board:gdp", [m], ttl=timedelta(minutes=30), now=NOW)
        await refresh_market_priority(s, now=NOW)
        assert (await _rows(s))["board"].tier == Tier.WATCHED


async def test_resolved_and_closed_markets_are_excluded(session_factory) -> None:
    async with session_factory() as s:
        resolved = await _market(s, "polymarket", "resolved", resolved_at=NOW - timedelta(days=1))
        closed = await _market(s, "polymarket", "closed", closes_at=NOW - timedelta(minutes=1))
        live = await _market(s, "polymarket", "live", closes_at=NOW + timedelta(days=1))
        await register_watch(
            s, "board:x", [resolved, closed, live], ttl=timedelta(hours=1), now=NOW
        )
        await refresh_market_priority(s, now=NOW)
        assert set(await _rows(s)) == {"live"}


async def test_expired_watch_is_ignored_and_pruned(session_factory) -> None:
    async with session_factory() as s:
        m = await _market(s, "polymarket", "expired")
        await register_watch(
            s, "board:x", [m], ttl=timedelta(minutes=1), now=NOW - timedelta(hours=1)
        )
        result = await refresh_market_priority(s, now=NOW)
        assert await _rows(s) == {}
        assert result.watches_expired == 1


async def test_demoted_rows_are_deleted_on_next_refresh(session_factory) -> None:
    async with session_factory() as s:
        m = await _market(s, "polymarket", "demote")
        await register_watch(s, "board:x", [m], ttl=timedelta(minutes=10), now=NOW)
        await refresh_market_priority(s, now=NOW)
        assert set(await _rows(s)) == {"demote"}
        later = NOW + timedelta(minutes=15)
        result = await refresh_market_priority(s, now=later)
        assert await _rows(s) == {}
        assert result.removed == 1


async def test_new_listing_tier_uses_the_id_watermark(session_factory) -> None:
    async with session_factory() as s:
        old = await _market(s, "kalshi", "old")
        # A watermark 3 days old covering `old`; anything above it is "new".
        s.add(CoreMarketIdWatermark(taken_at=NOW - timedelta(days=3), max_id=old))
        await s.flush()
        await _market(s, "kalshi", "new")
        result = await refresh_market_priority(s, now=NOW)
        rows = await _rows(s)

    assert set(rows) == {"new"}
    assert rows["new"].tier == Tier.NEW
    assert result.watermark_used == old


async def test_no_watermark_yet_means_no_new_tier(session_factory) -> None:
    async with session_factory() as s:
        await _market(s, "kalshi", "a")
        result = await refresh_market_priority(s, now=NOW)
        assert await _rows(s) == {}
        assert result.watermark_used is None
        # ...but this run sampled one, so the next run has a P1 clock.
        assert result.watermark_taken == 1


async def test_priority_ids_orders_by_tier_then_rank_and_filters_venue(session_factory) -> None:
    async with session_factory() as s:
        low = await _market(s, "polymarket", "low", last_volume_24h=1.0)
        high = await _market(s, "polymarket", "high", last_volume_24h=500.0)
        comp = await _market(s, "polymarket", "comp", last_volume_24h=0.0)
        other = await _market(s, "kalshi", "other")
        await _score_with_components(s, [comp])
        await register_watch(
            s, activity_watcher("polymarket"), [low, high], ttl=timedelta(hours=24), now=NOW
        )
        await register_watch(
            s, activity_watcher("kalshi"), [other], ttl=timedelta(hours=24), now=NOW
        )
        await refresh_market_priority(s, now=NOW)

        got = await priority_ids(s, "polymarket", Tier.ACTIVE, limit=10)
        watched_only = await priority_ids(s, "polymarket", Tier.WATCHED, limit=10)
        capped = await priority_ids(s, "polymarket", Tier.ACTIVE, limit=2)

    assert [p.external_id for p in got] == ["comp", "high", "low"]
    assert [p.external_id for p in watched_only] == ["comp"]
    assert [p.external_id for p in capped] == ["comp", "high"]


async def test_priority_ids_empty_table_is_empty_list(session_factory) -> None:
    async with session_factory() as s:
        assert await priority_ids(s, "polymarket", Tier.NEW, limit=100) == []
        assert await priority_ids(s, "polymarket", Tier.NEW, limit=0) == []


async def test_register_watch_dedupes_and_extends_expiry(session_factory) -> None:
    async with session_factory() as s:
        m = await _market(s, "polymarket", "dup")
        sent = await register_watch(s, "board:x", [m, m], ttl=timedelta(minutes=5), now=NOW)
        await register_watch(s, "board:x", [m], ttl=timedelta(hours=2), now=NOW)
        expires = (
            await s.execute(
                text("SELECT expires_at FROM core_market_watch WHERE market_id = :m"), {"m": m}
            )
        ).scalar_one()

    assert sent == 1
    assert expires == NOW + timedelta(hours=2)
