"""Fixtures for the Postgres queue / durable workflow tests (CORR-4.6 / 8.1).

Same strategy as pmi-api/tests/conftest.py: spin up an ephemeral
``pmi_workers_test`` database on the running ``postgres`` service,
``Base.metadata.create_all`` it, and point the test sessions there.

One extra trick this suite needs: ``pmi_core.workflow`` checkpoints through
the module-global ``pmi_core.db.SessionLocal`` (each step is its own short
transaction by design), so an autouse fixture monkeypatches that global onto
the test engine — workflow code under test then transparently writes to the
ephemeral DB.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from pmi_core.models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

TEST_DB_NAME = os.environ.get("PMI_TEST_DB_NAME", "pmi_workers_test")
DB_HOST = os.environ.get("PMI_TEST_DB_HOST", os.environ.get("PMI_DB_HOST", "postgres"))
DB_PORT = int(os.environ.get("PMI_TEST_DB_PORT", os.environ.get("PMI_DB_PORT", "5432")))
DB_USER = os.environ.get("PMI_TEST_DB_USER", os.environ.get("PMI_DB_USER", "pmi"))
DB_PASSWORD = os.environ.get(
    "PMI_TEST_DB_PASSWORD", os.environ.get("PMI_DB_PASSWORD", "pmi")
)


def _admin_url() -> str:
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/postgres"


def _test_url() -> str:
    return f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{TEST_DB_NAME}"


async def _create_test_database() -> None:
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


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncIterator[AsyncEngine]:
    await _create_test_database()
    engine = create_async_engine(_test_url(), poolclass=NullPool, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        await _drop_test_database()


@pytest_asyncio.fixture(scope="session")
async def session_factory(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=test_engine, expire_on_commit=False, autoflush=False
    )


@pytest.fixture(autouse=True)
def _patch_session_scope(monkeypatch, session_factory) -> None:
    """Route pmi_core.db.session_scope() (used by workflow checkpoints and
    queue convenience helpers) at the ephemeral test DB."""
    import pmi_core.db as core_db

    monkeypatch.setattr(core_db, "SessionLocal", session_factory)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(session_factory) -> None:
    """Each test starts from empty queue/workflow tables."""
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE core_workflow_steps, core_workflow_runs, core_jobs "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
