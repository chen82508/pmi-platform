"""Seat-board sugar over the platform-wide trend store.

The slim seat-board history (``senate-board-trend:*``) is now one instance of
the general mechanism in :mod:`pmi_core.engine.trend_store`; this module keeps
the seat-board-named helpers (they take a bare ``index_id`` and build the
``senate-board:*`` cache key) so the board snapshot jobs and readers are
unchanged. New verticals should use ``trend_store`` directly.
"""

from __future__ import annotations

from pmi_core.engine.trend_store import project, slim_key

BOARD_KEY_PREFIX = "senate-board:"


def board_key(index_id: str) -> str:
    return f"{BOARD_KEY_PREFIX}{index_id}"


def trend_key_for_board_key(cache_key: str) -> str:
    """``senate-board:X`` → ``senate-board-trend:X``."""
    if not cache_key.startswith(BOARD_KEY_PREFIX):
        raise ValueError(f"not a seat-board cache key: {cache_key!r}")
    return slim_key(cache_key)


def slim_payload(payload: dict) -> dict:
    """The history-relevant subset of a full board payload."""
    return project(board_key("_"), payload)


__all__ = ["BOARD_KEY_PREFIX", "board_key", "trend_key_for_board_key", "slim_payload"]
