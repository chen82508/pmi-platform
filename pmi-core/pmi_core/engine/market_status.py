"""Single source of truth for "is this market still live?".

The scoring selector and every read/display surface must agree on which markets
count as live. When they don't, a market drops out of the index *score* but keeps
showing up in the page's contract list (or the reverse). This module holds that
gate once, in SQL, so it composes into any ``select().where(...)``.

A market is LIVE when ALL of these hold:

* not resolution-stamped — ``resolved_at IS NULL``
* not past its close time — ``closes_at IS NULL OR closes_at > now()``
* the venue itself has not flagged it done:
    * polymarket — ``raw->>'closed'``     is not ``'true'``
    * manifold   — ``raw->>'isResolved'`` is not ``'true'``

The venue-flag clauses catch the gap where a market ends on the exchange before
our resolution sweeper stamps ``resolved_at`` (and before ``closes_at`` — which
several venues set unreliably — moves into the past).

Usage::

    from pmi_core.engine.market_status import live_market_conditions
    stmt = select(CoreMarket).where(*live_market_conditions())
"""

from __future__ import annotations

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.sql.elements import ColumnElement

from pmi_core.models import CoreMarket

# Per-venue "this market is done" flags in the vendor `raw` payload. Add a venue
# here (not at every call site) when its poller carries a definitive closed flag.
_VENUE_DONE_FLAGS: tuple[tuple[str, str], ...] = (
    ("polymarket", "closed"),
    ("manifold", "isResolved"),
)


def live_market_conditions() -> list[ColumnElement[bool]]:
    """AND-able conditions selecting markets that are still open/tradeable.

    Returns a list so callers splat it into ``.where(*live_market_conditions())``
    alongside their own predicates.
    """
    conditions: list[ColumnElement[bool]] = [
        CoreMarket.resolved_at.is_(None),
        or_(CoreMarket.closes_at.is_(None), CoreMarket.closes_at > func.now()),
    ]
    for venue, flag in _VENUE_DONE_FLAGS:
        conditions.append(
            not_(
                and_(
                    CoreMarket.venue == venue,
                    # ``->>`` (not ``[flag].astext``): ``raw`` is a generic JSON
                    # column, whose comparator has no ``.astext`` — the operator
                    # works on both json and jsonb. coalesce: a NULL ``raw``
                    # would turn the whole NOT(...) NULL and drop an open market.
                    func.coalesce(CoreMarket.raw.op("->>")(flag), "false") == "true",
                )
            )
        )
    return conditions
