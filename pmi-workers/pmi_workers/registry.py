"""Job registry — name → async callable lookup.

Ported verbatim from the legacy job executor's registry because the
contract (decorate-and-discover) is independent of the underlying engine.
The only change is the job signature: pmi-core jobs receive no `AsyncSession`
because the pmi-core pipeline manages its own `session_scope()` internally
(see [`pmi_core.engine.pipeline.run_pipeline`](../../pmi-core/pmi_core/engine/pipeline.py)).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

# Jobs must be async def functions. `Coroutine` (not the looser `Awaitable`)
# is required so callers can pass the result to `asyncio.run()`.
JobFn = Callable[..., Coroutine[Any, Any, Any]]

_registry: dict[str, JobFn] = {}
_queues: dict[str, str] = {}

_F = TypeVar("_F", bound=JobFn)

DEFAULT_QUEUE = "default"


def register(name: str, *, queue: str = DEFAULT_QUEUE) -> Callable[[_F], _F]:
    """Decorator: register a coroutine under `name` for `run-job <name>`.

    `queue` is the lane the job belongs in, and it belongs to the job rather
    than to whoever enqueues it: a whole-registry pass runs for hours, so one
    producer forgetting `--queue batch` parks it in the fast lane and every
    board snapshot queues behind it until it finishes. Declaring it here means
    cron lines, the API's on-demand path and the WS fan-out all route the same
    way by default. See `queue_for`.

    Lanes (one claim-loop container each, see docker-compose.yml):
      default  — on-demand `score` + the WS `reeval-market` fan-out
      snapshot — the */10 page snapshots a board/hub/home page serves from
      batch    — hourly / reeval-drifted / tag-* whole-registry passes
      long     — daily, tag-games, workflow: hours, serialised, never next to
                 the hourly pass (2026-10-20: `daily` parked it for 2.5 h)
    """

    def decorator(fn: _F) -> _F:
        if name in _registry:
            raise RuntimeError(f"Job '{name}' already registered")
        _registry[name] = fn
        _queues[name] = queue
        return fn

    return decorator


def get(name: str) -> JobFn:
    if name not in _registry:
        raise KeyError(f"Job '{name}' not registered")
    return _registry[name]


def queue_for(name: str) -> str:
    """The lane `name` declared at registration. Unknown jobs get the default
    lane — enqueuing an unregistered name is already an error the worker
    reports when it tries to run it."""
    return _queues.get(name, DEFAULT_QUEUE)


def all_names() -> list[str]:
    return sorted(_registry.keys())
