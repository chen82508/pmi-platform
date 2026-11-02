"""Incumbent party per contested seat — which party held the seat before the
election, so the board can mark HELD BY and flag flips (projected winner of the
other party).

Loaded from pmi_core/config/incumbency.yaml (see the file header for sources).
Static reference data, so consumers read it at serve time — it is never written
to the shared data plane. Senate/governor are keyed by 2-letter state; house is
keyed per-district (STATE-DISTRICT, e.g. CA-20) and IS compiled (119th Congress)
for the zero-liquidity seat floor. The board's HELD BY still passes a 2-letter
state, so house HELD BY resolves to None there until that path is district-aware.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml as _yaml_mod
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

_PATH = Path(__file__).parent.parent / "config" / "incumbency.yaml"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not _PATH.exists() or not _YAML_OK:
        _cache = {}
        return _cache
    with open(_PATH) as f:
        _cache = _yaml_mod.safe_load(f) or {}
    return _cache


def _norm_seat(key: str) -> str:
    """Normalize a seat key so callers with different district paddings resolve to
    the same roster entry. The roster is keyed unpadded (the aggregator's race
    code, e.g. ``AK-1``/``CA-20``), but the board serves zero-padded district codes
    (``AK-01``); strip the pad on the trailing district segment. 2-letter states
    (``NC``) and non-numeric tails pass through unchanged."""
    key = key.upper()
    state, sep, dist = key.rpartition("-")
    return f"{state}-{int(dist)}" if sep and dist.isdigit() else key


def incumbent_party(office: str, state_code: str, cycle_year: int) -> str | None:
    """Party ("R"|"D") holding the (office, state|district) seat before cycle_year,
    or None when unlisted. Case- and district-padding-insensitive: senate/governor
    are keyed by 2-letter state, house per-district (``CA-20``); ``AK-01`` resolves
    to the roster's ``AK-1``."""
    if not office or not state_code:
        return None
    by_cycle = _load().get(office, {})
    by_state = by_cycle.get(cycle_year) or {}
    party = by_state.get(_norm_seat(state_code))
    return party if party in ("R", "D") else None


def is_open_seat(office: str, state_code: str, cycle_year: int) -> bool:
    """Is the (office, state) seat OPEN in cycle_year — i.e. the incumbent is not
    running (term-limited, retiring, lost the primary, or seeking another office)?

    An open seat is still HELD BY its party (see ``incumbent_party``) but has no
    running incumbent, so a zero-liquidity prior must not assume the party holds —
    an open seat can flip. Uncompiled offices (house) list nothing → always False;
    callers fall back to the thin market read via the None party path anyway.
    Case-insensitive on the state code.
    """
    if not office or not state_code:
        return False
    by_cycle = _load().get("open_seats", {}).get(office, {})
    return _norm_seat(state_code) in (by_cycle.get(cycle_year) or [])
