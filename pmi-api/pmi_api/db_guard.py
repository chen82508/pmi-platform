"""Per-query time budgets that fail one query, not the whole request.

The connection-wide statement timeout (PMI_DB_STATEMENT_TIMEOUT_MS, 30 s)
protects the pool, but when it fires the request 500s. Several reads are
optional parts of a response (a page's trend line), so ``bounded`` runs one
statement group inside a savepoint with its own, shorter ``SET LOCAL
statement_timeout``. On timeout only the savepoint rolls back — the request's
session stays usable — and the caller gets ``QueryTimeout`` to degrade on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

# Postgres SQLSTATE for "canceling statement due to statement timeout".
QUERY_CANCELED = "57014"


class QueryTimeout(Exception):
    """A bounded query ran past its budget and was cancelled."""


def is_statement_timeout(exc: BaseException) -> bool:
    """True when ``exc`` (or anything it wraps) is a statement cancellation."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "sqlstate", None) == QUERY_CANCELED:
            return True
        if type(cur).__name__ == "QueryCanceledError":
            return True
        cur = getattr(cur, "orig", None) or cur.__cause__
    return False


async def bounded(
    session: AsyncSession, timeout_ms: int, op: Callable[[], Awaitable[T]]
) -> T:
    """Run ``op`` (which uses ``session``) under ``timeout_ms``.

    ``SET LOCAL`` inside the savepoint is undone when the savepoint rolls back,
    and reset to the connection default before a successful release, so the
    rest of the transaction keeps the normal statement timeout.
    """
    if timeout_ms <= 0:
        return await op()
    try:
        async with session.begin_nested():
            await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            result = await op()
            await session.execute(text("SET LOCAL statement_timeout TO DEFAULT"))
            return result
    except DBAPIError as exc:
        if is_statement_timeout(exc):
            raise QueryTimeout(str(exc.orig) if exc.orig is not None else str(exc)) from exc
        raise
