"""Shared fixtures for pmi-api route tests.

Strategy
--------
The pmi-core schema relies on Postgres-only column types (``ARRAY(BigInteger)``
in ``ts_index_scores.component_evaluation_ids`` and pgvector ``Vector(1536)``
in ``vec_market_embeddings``), so we cannot fall back to SQLite for these
tests. Instead we:

1. Connect to the running ``postgres`` service (the same container the dev
   stack uses), open the admin DB, and ``CREATE DATABASE pmi_api_test`` for
   each session.
2. Connect to ``pmi_api_test`` with the async engine, ``CREATE EXTENSION
   vector`` + ``CREATE EXTENSION pg_trgm``, then run
   ``Base.metadata.create_all(engine)`` — fast and migration-free for
   read-only API tests.
3. Seed minimal fixtures (one index def, one market, one score, a price
   snapshot, four audit_evaluations) in a session-scoped fixture so the
   route tests can assert against deterministic values.
4. Wire FastAPI ``dependency_overrides[get_session]`` to yield from the
   test engine. The ``httpx.AsyncClient(transport=ASGITransport(app=app))``
   pattern keeps the tests fully in-process.
5. On teardown, dispose the engine and drop ``pmi_api_test``.

The test DB host/port come from ``PMI_TEST_DB_*`` env vars (with
``PMI_DB_*`` fallback) so the same suite runs both inside the compose
network (``host=postgres``) and from the host (``host=localhost``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pmi_core.models import (
    AuditEvaluation,
    Base,
    CoreApiKey,
    CoreIndexDefinition,
    CoreMarket,
    CorePrompt,
    TsIndexScore,
    TsPriceSnapshot,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

TEST_DB_NAME = os.environ.get("PMI_TEST_DB_NAME", "pmi_api_test")
DB_HOST = os.environ.get("PMI_TEST_DB_HOST", os.environ.get("PMI_DB_HOST", "postgres"))
DB_PORT = int(os.environ.get("PMI_TEST_DB_PORT", os.environ.get("PMI_DB_PORT", "5432")))
DB_USER = os.environ.get("PMI_TEST_DB_USER", os.environ.get("PMI_DB_USER", "pmi"))
DB_PASSWORD = os.environ.get(
    "PMI_TEST_DB_PASSWORD", os.environ.get("PMI_DB_PASSWORD", "pmi")
)


def _admin_url() -> str:
    # Connect to the always-present `postgres` system DB to CREATE/DROP the test DB.
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/postgres"


def _test_url() -> str:
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{TEST_DB_NAME}"


async def _create_test_database() -> None:
    """Drop and recreate the test database. Idempotent."""
    admin_engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        await admin_engine.dispose()


async def _drop_test_database() -> None:
    admin_engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    finally:
        await admin_engine.dispose()


async def _bootstrap_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)


# ──────────────────────────────────────────────────────────────────────────
# Event loop — keep a single loop across the whole test session so async
# engines created in session-scoped fixtures don't outlive their loop.
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


# ──────────────────────────────────────────────────────────────────────────
# DB engine + schema (session-scoped, one-shot per pytest run)
# ──────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncIterator[AsyncEngine]:
    """Engine bound to a freshly-minted ``pmi_api_test`` database.

    Uses ``NullPool`` so each checkout opens a fresh asyncpg connection on
    the current event loop. This is essential under pytest-asyncio, which
    creates a new event loop per test function — pooled connections would
    otherwise raise "Future attached to a different loop" RuntimeError on
    the second test.
    """
    await _create_test_database()
    engine = create_async_engine(_test_url(), poolclass=NullPool, echo=False)
    try:
        await _bootstrap_schema(engine)
        yield engine
    finally:
        await engine.dispose()
        await _drop_test_database()


@pytest_asyncio.fixture(scope="session")
async def session_factory(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)


# ──────────────────────────────────────────────────────────────────────────
# Fixture data — deterministic so route tests can assert on exact values
# ──────────────────────────────────────────────────────────────────────────

FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="session")
async def seeded_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Populate the test DB once and return the inserted IDs / known values.

    Two indexes are seeded so the list/get/score/history/explain tests can
    cover both the happy path (``polymarket-war-index``) and edge cases
    (``empty-index``: no score yet, used for 404 assertions).

    Score numbers below match the analytic expectation for the seeded data:
    one market with relevancy = 1.0, direction = +1, price = 0.60 →
    raw = 0.10 → score = 50 + 0.10 * 50 = 55.0.
    """
    async with session_factory() as session:
        # --- core_prompts ----------------------------------------------------
        prompt = CorePrompt(
            name="directly_about_war",
            version=1,
            template="dummy prompt body",
            sha256="a" * 64,
        )
        session.add(prompt)
        await session.flush()

        # --- core_index_definitions -----------------------------------------
        war_ir = {
            "id": "polymarket-war-index",
            "version": 1,
            "title": "Polymarket War Index",
            "owner": "test",
            "selectors": [{"type": "keyword", "terms": ["war"]}],
            "factors": [
                {
                    "id": "directly_about_war",
                    "type": "binary",
                    "prompt_ref": "prompts/factors/directly_about_war-v1",
                    "weight": 1.0,
                },
                {
                    "id": "direction",
                    "type": "ternary",
                    "prompt_ref": "prompts/factors/direction-v1",
                    "weight": None,
                },
            ],
        }
        war_def = CoreIndexDefinition(
            index_id="polymarket-war-index",
            version=1,
            title="Polymarket War Index",
            owner="test",
            definition=war_ir,
            yaml_source="id: polymarket-war-index\nversion: 1\n",
            yaml_sha256="b" * 64,
            is_current=True,
            effective_from=FIXED_NOW - timedelta(days=10),
        )
        empty_def = CoreIndexDefinition(
            index_id="empty-index",
            version=1,
            title="Empty Index (no score)",
            owner="test",
            definition={
                **war_ir,
                "id": "empty-index",
                "title": "Empty Index (no score)",
            },
            yaml_source="id: empty-index\nversion: 1\n",
            yaml_sha256="c" * 64,
            is_current=True,
            effective_from=FIXED_NOW - timedelta(days=10),
        )
        session.add_all([war_def, empty_def])
        await session.flush()

        # --- core_markets ----------------------------------------------------
        market = CoreMarket(
            venue="polymarket",
            external_id="seed-war-1",
            slug="will-x-happen",
            title="Will the war end this year?",
            description="Test market",
        )
        session.add(market)
        await session.flush()

        # --- ts_price_snapshots: one recent, one older. The /explain route
        # should pick the one at-or-before score.as_of (the recent one).
        score_as_of = FIXED_NOW - timedelta(hours=1)
        session.add_all([
            TsPriceSnapshot(
                market_id=market.id,
                snapshot_at=score_as_of - timedelta(minutes=5),
                last_price=0.60,
                volume_24h=12345.0,
            ),
            TsPriceSnapshot(
                market_id=market.id,
                snapshot_at=score_as_of - timedelta(days=3),
                last_price=0.20,
                volume_24h=200.0,
            ),
        ])

        # --- audit_evaluations: relevancy + direction so /explain can
        # reconstruct the score breakdown.
        rel_eval = AuditEvaluation(
            market_id=market.id,
            index_definition_id=war_def.id,
            factor_id="directly_about_war",
            prompt_id=prompt.id,
            prompt_sha256=prompt.sha256,
            model_id="stub:hash-v0",
            value_numeric=1.0,
            confidence=0.95,
            model_response={"value": 1, "source": "test"},
        )
        dir_eval = AuditEvaluation(
            market_id=market.id,
            index_definition_id=war_def.id,
            factor_id="direction",
            prompt_id=prompt.id,
            prompt_sha256=prompt.sha256,
            model_id="stub:hash-v0",
            value_numeric=1.0,
            value_label="+",
            confidence=0.90,
            model_response={"value": 1, "source": "test"},
        )
        session.add_all([rel_eval, dir_eval])
        await session.flush()

        # --- ts_index_scores: two points so /history returns >1 row.
        latest_score = TsIndexScore(
            index_definition_id=war_def.id,
            as_of=score_as_of,
            score=55.0,
            component_count=1,
            component_evaluation_ids=[rel_eval.id, dir_eval.id],
            breakdown={"raw": 0.10, "components_after_collapse": 1},
        )
        older_score = TsIndexScore(
            index_definition_id=war_def.id,
            as_of=score_as_of - timedelta(days=2),
            score=42.5,
            component_count=1,
            component_evaluation_ids=[],
            breakdown={"raw": -0.15, "components_after_collapse": 1},
        )
        session.add_all([latest_score, older_score])

        # --- Senate board fixture (SHIP-2.5 + CORR-1.3) ---------------------
        # Three component markets, but only TWO are party-direct per-state
        # races that seat_mapping (CORR-1.3) recognises; the third is nominee
        # noise that must be filtered OUT of the contested set. The two real
        # seats (North Carolina R, Texas R) are both priced at 0.50 (P(R wins)
        # = 50%), plus holdover 49R / 49D. NC and TX both genuinely hold a 2026
        # senate election (race_calendar.yaml), so the /maga calendar gate keeps
        # them — keeping this fixture realistic for the senate-board AND the
        # MAGA route tests. The Poisson-binomial seat distribution is then
        # analytic and deterministic:
        #   contested PMF = [0.25, 0.50, 0.25]  → total R seats 49/50/51
        #   P(R majority ≥51) = 0.25 → 25.0% ;  P(D majority) = 0.25 → 25.0%
        #   E[R seats] = 49 + 1.0 = 50.0 ; both seats classify as "tossup".
        senate_markets = [
            ("Will the Republicans win the North Carolina Senate race in 2026?", 0.50, True),
            ("Will the Republicans win the Texas Senate race in 2026?", 0.50, True),
            # noise: a nominee market that the keyword selector also matched but
            # seat_mapping must reject (not a party-direct general-election race).
            ("Will Jane Doe be the Democratic nominee for Senate in North Carolina?", 0.30, False),
        ]
        senate_ir = {
            "id": "us-senate-2026-republican-seats",
            "version": 1,
            "title": "US Senate 2026 — Projected Republican Seats (#)",
            "owner": "test",
            "selectors": [{"type": "keyword", "terms": ["senate"]}],
            "factors": [
                {
                    "id": "republican_on_yes",
                    "type": "binary",
                    "prompt_ref": "prompts/factors/republican-on-yes-v1",
                    "weight": 1.0,
                },
            ],
            "aggregation": {
                "formula": "seat_projection_sum",
                "seat_projection": {
                    "total_seats": 100,
                    "majority_threshold": 51,
                    "holdover_r": 49,
                    "holdover_d": 49,
                },
            },
        }
        senate_def = CoreIndexDefinition(
            index_id="us-senate-2026-republican-seats",
            version=1,
            title="US Senate 2026 — Projected Republican Seats (#)",
            owner="test",
            definition=senate_ir,
            yaml_source="id: us-senate-2026-republican-seats\nversion: 1\n",
            yaml_sha256="f" * 64,
            is_current=True,
            effective_from=FIXED_NOW - timedelta(days=10),
        )
        session.add(senate_def)
        await session.flush()

        senate_eval_ids: list[int] = []
        for i, (title, price, _is_seat) in enumerate(senate_markets):
            seat_market = CoreMarket(
                venue="polymarket",
                external_id=f"seed-senate-{i}",
                slug=f"senate-race-{i}",
                title=title,
                description="Test senate market",
            )
            session.add(seat_market)
            await session.flush()
            session.add(
                TsPriceSnapshot(
                    market_id=seat_market.id,
                    snapshot_at=score_as_of - timedelta(minutes=5),
                    last_price=price,
                    volume_24h=1000.0 * (i + 1),
                )
            )
            seat_eval = AuditEvaluation(
                market_id=seat_market.id,
                index_definition_id=senate_def.id,
                factor_id="republican_on_yes",
                prompt_id=prompt.id,
                prompt_sha256=prompt.sha256,
                model_id="stub:hash-v0",
                value_numeric=1.0,
                confidence=0.9,
                model_response={"value": 1, "source": "test"},
            )
            session.add(seat_eval)
            await session.flush()
            senate_eval_ids.append(seat_eval.id)

        senate_score = TsIndexScore(
            index_definition_id=senate_def.id,
            as_of=score_as_of,
            score=50.0,
            component_count=2,
            component_evaluation_ids=senate_eval_ids,
            breakdown={"raw": 0.0},
        )
        session.add(senate_score)

        # --- null-score fixture: a dedicated index def with ONE null-score row
        # and ONE real-score row, so route tests can assert the null surfaces
        # through /score, /score/history and /explain instead of crashing on
        # float(None). Backport of Micah PR #316 / job-executor PR #12 — see
        # alembic 0006 and the aggregator's "below min_components" path.
        null_ir = {
            **war_ir,
            "id": "null-score-index",
            "title": "Null Score Index",
        }
        null_def = CoreIndexDefinition(
            index_id="null-score-index",
            version=1,
            title="Null Score Index",
            owner="test",
            definition=null_ir,
            yaml_source="id: null-score-index\nversion: 1\n",
            yaml_sha256="0" * 64,
            is_current=True,
            effective_from=FIXED_NOW - timedelta(days=10),
        )
        session.add(null_def)
        await session.flush()

        null_latest = TsIndexScore(
            index_definition_id=null_def.id,
            as_of=score_as_of,
            score=None,
            component_count=0,
            component_evaluation_ids=[],
            breakdown={"reason": "below min_components", "candidates": 0},
        )
        null_older = TsIndexScore(
            index_definition_id=null_def.id,
            as_of=score_as_of - timedelta(days=2),
            score=33.3,
            component_count=1,
            component_evaluation_ids=[],
            breakdown={"raw": -0.334},
        )
        session.add_all([null_latest, null_older])

        # --- core_api_keys: one active key + one revoked, for auth tests.
        active_key = CoreApiKey(
            key_prefix="pmi_test_",
            key_hash="d" * 64,  # placeholder — auth tests pass key_hash directly
            label="active",
            is_active=True,
        )
        revoked_key = CoreApiKey(
            key_prefix="pmi_dead_",
            key_hash="e" * 64,
            label="revoked",
            is_active=False,
        )
        session.add_all([active_key, revoked_key])

        await session.commit()

        return {
            "war_def_id": war_def.id,
            "empty_def_id": empty_def.id,
            "market_id": market.id,
            "prompt_id": prompt.id,
            "rel_eval_id": rel_eval.id,
            "dir_eval_id": dir_eval.id,
            "latest_score_as_of": score_as_of,
            "older_score_as_of": score_as_of - timedelta(days=2),
            "expected_score": 55.0,
            "older_expected_score": 42.5,
            "active_key_hash": "d" * 64,
            "revoked_key_hash": "e" * 64,
            "senate_def_id": senate_def.id,
            "senate_index_id": "us-senate-2026-republican-seats",
            "null_def_id": null_def.id,
            "null_index_id": "null-score-index",
            "null_older_expected_score": 33.3,
        }


# ──────────────────────────────────────────────────────────────────────────
# FastAPI app + AsyncClient
# ──────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_data: dict[str, Any],
) -> AsyncIterator[AsyncClient]:
    """Yield an httpx AsyncClient wired to the FastAPI app + test DB."""
    # Import locally so app construction doesn't touch the real DB module
    # before our overrides are installed.
    from pmi_api.deps import get_session, require_api_key
    from pmi_api.main import app

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_require_api_key() -> None:
        # CORR-0.6: routes are now mounted with ``Depends(require_api_key)``.
        # Existing route tests exercise handler logic, not the auth gate, so
        # short-circuit to "auth disabled" here (the dev `.env.example` ships
        # ``PMI_API_REQUIRE_AUTH=false`` for the same reason). The real auth
        # behaviour against the live routes lives in test_routes_auth.py.
        return None

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[require_api_key] = override_require_api_key
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
