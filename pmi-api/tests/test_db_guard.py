"""db_guard against the test Postgres: a bounded query times out on its own
budget, the request's session stays usable, and the connection's normal
statement timeout comes back afterwards."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from pmi_api.db_guard import QueryTimeout, bounded, is_statement_timeout

# Explicit, like the other route tests, so this runs under strict asyncio mode too.
pytestmark = pytest.mark.asyncio


async def _statement_timeout(session) -> str:
    return (await session.execute(text("SHOW statement_timeout"))).scalar_one()


async def test_timeout_raises_and_leaves_the_session_usable(session_factory):
    async with session_factory() as session:
        before = await _statement_timeout(session)

        async def slow():
            await session.execute(text("SELECT pg_sleep(2)"))

        with pytest.raises(QueryTimeout):
            await bounded(session, 50, slow)
        # Only the savepoint rolled back: the outer transaction still works and
        # the per-query budget did not leak into it.
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        assert await _statement_timeout(session) == before


async def test_success_restores_the_connection_default(session_factory):
    async with session_factory() as session:
        before = await _statement_timeout(session)

        async def fast():
            return (await session.execute(text("SELECT 42"))).scalar_one()

        assert await bounded(session, 5000, fast) == 42
        assert await _statement_timeout(session) == before


async def test_other_database_errors_are_not_treated_as_timeouts(session_factory):
    async with session_factory() as session:

        async def broken():
            await session.execute(text("SELECT 1/0"))

        with pytest.raises(DBAPIError) as excinfo:
            await bounded(session, 5000, broken)
        assert not is_statement_timeout(excinfo.value)


async def test_connection_wide_timeout_is_recognised(session_factory):
    async with session_factory() as session:
        await session.execute(text("SET statement_timeout = 50"))
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(text("SELECT pg_sleep(2)"))
        assert is_statement_timeout(excinfo.value)
