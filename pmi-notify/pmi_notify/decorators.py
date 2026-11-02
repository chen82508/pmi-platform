"""Decorators — orchestration only.

`notify_on_failure` and `notify_on_success` know nothing about Slack (or any
other vendor): they build a vendor-neutral `NotificationMessage` and hand it
to a `BaseNotificationAdapter` (injected, or resolved from `factory.get_notifier()`).
This is the Dependency Inversion in practice — swap the adapter, the
decorators don't change.

Both decorators:
  * wrap `async def` and plain `def` targets, preserving signature/metadata
    via `functools.wraps`
  * NEVER let a notification failure affect the wrapped function's own
    result or exception — dispatch is isolated in `_dispatch`
  * are safe to stack / reuse across sync and async call sites in the same
    process

`build_failure_message` is also exported standalone for callers that already
own their own try/except boundary (e.g. a base class whose `run_once()`
converts the original exception into a different type before re-raising) and
therefore want to build+send a failure alert directly, without wrapping a
function in `notify_on_failure`.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ParamSpec, TypeVar

import structlog

from pmi_notify.base import BaseNotificationAdapter
from pmi_notify.factory import get_notifier
from pmi_notify.message import NotificationLevel, NotificationMessage
from pmi_notify.traceback_filter import filter_traceback

log = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


async def _dispatch(notifier: BaseNotificationAdapter, message: NotificationMessage) -> None:
    """Send `message`, isolating ANY failure from the caller.

    `BaseNotificationAdapter.send()` already contracts to never raise, but we
    guard here too — a bug in a third-party adapter implementation must still
    never take down the function it's decorating.
    """
    try:
        await notifier.send(message)
    except Exception:  # noqa: BLE001 - notification delivery is best-effort by design
        log.warning("notify.dispatch_failed", exc_info=True)


def build_failure_message(
    display_name: str,
    exc: BaseException,
    *,
    mention: str | None = None,
    include_root: str | None = None,
) -> NotificationMessage:
    """Build a rich, scannable failure alert: bolded error type/message, a
    project-native-only filtered traceback in a code block, and
    timestamp/environment fields for at-a-glance triage.

    `include_root` scopes the traceback filter to a specific package
    directory (see `traceback_filter.filter_traceback`) — pass it when the
    caller knows its own source root and wants stricter filtering than the
    generic `.venv`/`site-packages` exclusion list alone provides.
    """
    filtered = filter_traceback(exc, include_root=include_root)
    return NotificationMessage(
        title=f"{display_name} failed",
        body=f"❌ *{exc.__class__.__name__}*: {exc}\n\n💻 ```python\n{filtered}\n```",
        level=NotificationLevel.ERROR,
        mention=mention,
        fields={
            "Timestamp (UTC)": datetime.now(UTC).isoformat(timespec="seconds"),
            "Environment": os.environ.get("SLACK_ENVIRONMENT", "development"),
        },
        source=display_name,
    )


def _success_message(
    display_name: str, mention: str | None, result: Any, include_result: bool
) -> NotificationMessage:
    return NotificationMessage(
        title=f"{display_name} succeeded",
        body=f"```{result!r}```" if include_result else "",
        level=NotificationLevel.SUCCESS,
        mention=mention,
        source=display_name,
    )


def notify_on_failure(
    *,
    notifier: BaseNotificationAdapter | None = None,
    mention: str | None = None,
    channels: list[str] | None = None,
    title: str | None = None,
    reraise: bool = True,
    include_root: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Alert when the wrapped function raises.

    By default the original exception is re-raised after the alert is sent —
    this decorator adds observability, it does not replace the caller's own
    error handling. Set `reraise=False` to swallow the exception instead
    (the function then returns None).

    `channels` is accepted for forward compatibility with adapters that can
    route per-call (e.g. a future bot-token Slack adapter); a plain Slack
    incoming webhook is bound to one channel, so it is currently advisory
    only and logged, not enforced.

    `include_root` is forwarded to `build_failure_message` / `filter_traceback`
    to scope the alert's stack trace to a specific package directory.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        display_name = title or fn.__qualname__
        if channels:
            log.info("notify.channels_advisory_only", fn=display_name, channels=channels)

        if asyncio.iscoroutinefunction(fn):
            async_fn = fn

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                try:
                    return await async_fn(*args, **kwargs)  # type: ignore[no-any-return]
                except Exception as exc:
                    active_notifier = notifier or get_notifier()
                    message = build_failure_message(
                        display_name, exc, mention=mention, include_root=include_root
                    )
                    await _dispatch(active_notifier, message)
                    if reraise:
                        raise
                    return None  # type: ignore[return-value]

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                active_notifier = notifier or get_notifier()
                message = build_failure_message(
                    display_name, exc, mention=mention, include_root=include_root
                )
                try:
                    asyncio.run(_dispatch(active_notifier, message))
                except Exception:  # noqa: BLE001 - see _dispatch docstring
                    log.warning("notify.dispatch_failed", exc_info=True)
                if reraise:
                    raise
                return None  # type: ignore[return-value]

        return sync_wrapper

    return decorator


def notify_on_success(
    *,
    notifier: BaseNotificationAdapter | None = None,
    mention: str | None = None,
    channels: list[str] | None = None,
    title: str | None = None,
    include_result: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Alert once the wrapped function completes without raising.

    Exceptions from the wrapped function propagate untouched — this
    decorator only reacts to clean completion. Set `include_result=True` to
    embed `repr(result)` in the alert body (careful with large/sensitive
    return values).
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        display_name = title or fn.__qualname__
        if channels:
            log.info("notify.channels_advisory_only", fn=display_name, channels=channels)

        if asyncio.iscoroutinefunction(fn):
            async_fn = fn

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                result = await async_fn(*args, **kwargs)
                active_notifier = notifier or get_notifier()
                message = _success_message(display_name, mention, result, include_result)
                await _dispatch(active_notifier, message)
                return result  # type: ignore[no-any-return]

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            result = fn(*args, **kwargs)
            active_notifier = notifier or get_notifier()
            message = _success_message(display_name, mention, result, include_result)
            try:
                asyncio.run(_dispatch(active_notifier, message))
            except Exception:  # noqa: BLE001 - see _dispatch docstring
                log.warning("notify.dispatch_failed", exc_info=True)
            return result

        return sync_wrapper

    return decorator


__all__ = ["build_failure_message", "notify_on_failure", "notify_on_success"]
