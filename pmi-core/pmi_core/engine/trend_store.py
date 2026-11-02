"""trend_store — the platform-wide "slim trend" store for ts_page_cache.

Problem (uniform across every vertical): each ``*-snapshot`` job writes one big
JSONB board blob per tick (war's national board, the GDP board, a climate board,
the MAGA cycle board — tens to hundreds of kB, TOAST-compressed). A trend line
only needs ONE scalar per hour out of it, but reading it back with
``page_cache.scalar_points`` had to scan every row in the window and detoast the
WHOLE blob just to pull that scalar. A 90-day hourly read detoasted tens of MB
and blew past the API statement timeout on a busy box (gdp ``/board``, war
country/world, climate, maga per-state — 2026-10). The seat boards already
solved this with a slim companion row (``board_trend``); this module is that
mechanism generalised so every vertical shares one implementation.

Fix (one mechanism for all): each snapshot ALSO appends a slim companion row
under :func:`slim_key` carrying only the fields trend readers extract
(:func:`project`). Readers query the slim key and fall back to the full key only
for the part of a window the slim series does not reach yet — it accrues forward
from deploy, and ``board-trend-backfill`` seeds it from existing full history.

Window: every trend reader is capped at :data:`TREND_DAYS` (90). The old 400-day
options are gone — retention thins rows past 90d to one/day anyway.

A projection spec is a tuple of path patterns; ``"*"`` matches any dict key at
that level, and everything from a matched node downward is kept. Flat scalars
(``("score",)``) and per-entity scalars (``("countries", "*", "heat")``) both
express cleanly, so one :func:`project` covers the flat boards and the nested
war/maga ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Every vertical trend reader is capped at this window; `le=400` is retired.
TREND_DAYS = 90

# cache-key family (text before the first ':') → the slim projection its trend
# readers need. A family with no entry gets no slim companion (it is a
# latest-render-only key). Each pattern's tokens are literal keys or '*' (any
# key); everything from a matched node downward is kept.
TREND_SPECS: dict[str, tuple[tuple[str, ...], ...]] = {
    "war": (
        ("version",),
        ("world_heat",),
        ("world_volume_24h",),
        ("countries", "*", "heat"),
        ("countries", "*", "volume_24h"),
        ("regions", "*", "heat"),
        ("regions", "*", "volume_24h"),
    ),
    "maga": (
        ("version",),
        ("national_heat",),
        ("chambers", "*", "pmi"),
        ("states", "*", "heat"),
        ("states", "*", "office_heat", "*"),
        ("states", "*", "district_heat", "*"),
    ),
    "gdp-board": (("version",), ("score",)),
    "climate-hub": (("version",), ("score",)),
    "climate-board": (("version",), ("score",)),
    "climate-question": (("version",), ("reading",)),
    "esports-match": (("match", "volume_24h"),),
    # Seat boards (senate/house/governor share this family via `senate-board:*`).
    "senate-board": (
        ("version",),
        ("p_r_control",),
        ("prob_by_state",),
        ("prob_by_district",),
    ),
}

# Token inserted into a cache key to name its slim companion.
_TREND_TOKEN = "-trend"


def family(cache_key: str) -> str:
    """The cache-key family: text before the first ``:`` ("war:2026" → "war",
    "climate-hub" → "climate-hub")."""
    return cache_key.split(":", 1)[0]


def has_trend(cache_key: str) -> bool:
    """True if this key's family keeps a slim trend companion."""
    return family(cache_key) in TREND_SPECS


def slim_key(cache_key: str) -> str:
    """Companion slim key: ``-trend`` suffixed onto the family token.

    "war:2026" → "war-trend:2026", "climate-hub" → "climate-hub-trend",
    "senate-board:X" → "senate-board-trend:X", "maga:2026" → "maga-trend:2026"
    — the last two match the keys those verticals already wrote, so their history
    and retention class carry over unchanged.
    """
    head, sep, rest = cache_key.partition(":")
    return f"{head}{_TREND_TOKEN}{sep}{rest}"


def _patterns(cache_key: str) -> tuple[tuple[str, ...], ...]:
    return TREND_SPECS.get(family(cache_key), ())


def covers(cache_key: str, path: tuple[str, ...]) -> bool:
    """True if ``path`` is kept by the family's slim projection, so a slim read
    for it is complete. Readers guard on this to avoid silently reading a path
    the slim row never stored (which would look like an empty series)."""
    return any(_path_matches(pat, path) for pat in _patterns(cache_key))


def _path_matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    # Covered when the pattern is a prefix of the requested path: the projection
    # keeps everything from the matched node downward, so a reader path at or
    # below it lands in kept data. A path shorter than the pattern (a non-leaf
    # parent) is NOT covered — trend readers always extract a scalar leaf.
    if len(pattern) > len(path):
        return False
    return all(pt == "*" or pt == pa for pt, pa in zip(pattern, path, strict=False))


def project(cache_key: str, payload: dict) -> dict:
    """The slim subtree of ``payload`` for its family's trend readers."""
    out: dict[str, Any] = {}
    for pat in _patterns(cache_key):
        _graft(payload, pat, out)
    return out


def _graft(src: Any, pattern: tuple[str, ...], dst: dict) -> None:
    if not isinstance(src, dict):
        return
    token, rest = pattern[0], pattern[1:]
    keys = list(src.keys()) if token == "*" else ([token] if token in src else [])
    for k in keys:
        if rest:
            child = dst.setdefault(k, {})
            if isinstance(child, dict):
                _graft(src[k], rest, child)
        else:
            dst[k] = src[k]  # leaf scalar or whole subtree — reference is fine


def slim_values(cache_key: str, payload: dict, computed_at: datetime) -> dict | None:
    """Insert-values for the slim companion row, or ``None`` when the family has
    no trend spec. Snapshot jobs::

        if (row := slim_values(cache_key, payload, computed_at)):
            session.add(TsPageCache(**row))
    """
    if not has_trend(cache_key):
        return None
    return {
        "cache_key": slim_key(cache_key),
        "payload": project(cache_key, payload),
        "computed_at": computed_at,
    }


def source_families() -> list[str]:
    """The FULL source key families that keep a slim companion — i.e. every
    ``TREND_SPECS`` key. Slim keys (``<family>-trend``) are never themselves
    families here, so the backfill filters on ``split_part(cache_key, ':', 1)
    IN source_families()`` to pick only the full rows to project."""
    return list(TREND_SPECS)
