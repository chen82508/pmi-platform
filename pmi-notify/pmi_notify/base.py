"""The Dependency Inversion seam: `decorators.py` depends only on
`BaseNotificationAdapter`, never on a concrete vendor. Swapping Slack for
Teams/Discord/PagerDuty is "implement this ABC + add a factory branch" — the
decorator layer never changes (Open-Closed).

Contract every adapter MUST honour: `send()` never raises. A broken webhook,
a DNS failure, a 500 from the vendor — all of it is reported via the boolean
return value and a logged warning, never an exception. This is what lets
`decorators.py` guarantee a notification failure can never crash the
function it's wrapping.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import structlog

from pmi_notify.message import NotificationMessage

log = structlog.get_logger(__name__)


class BaseNotificationAdapter(ABC):
    """Vendor-neutral notification sink."""

    @abstractmethod
    async def send(self, message: NotificationMessage) -> bool:
        """Deliver `message`. Returns True on success, False on any failure.

        Implementations must catch their own transport errors — never let an
        exception escape this method.
        """
        ...

    def send_sync(self, message: NotificationMessage) -> bool:
        """Synchronous bridge over `send()`, for non-async call sites.

        Raises RuntimeError if called from inside a running event loop —
        in that context the caller should `await send()` directly instead.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.send(message))
        raise RuntimeError(
            "send_sync() cannot be called from a running event loop; use `await send()` instead"
        )


class NullNotificationAdapter(BaseNotificationAdapter):
    """No-op sink used when no vendor is configured (e.g. no webhook URL).

    Exists so `factory.build_notifier()` always returns a usable adapter —
    callers never need to check "is notification configured?" themselves.
    Warns once per instance so misconfiguration is visible in logs without
    spamming them on every call.
    """

    def __init__(self) -> None:
        self._warned = False

    async def send(self, message: NotificationMessage) -> bool:
        if not self._warned:
            log.warning(
                "notify.unconfigured",
                detail="no notification adapter configured — set SLACK_WEBHOOK_URL to enable delivery",
            )
            self._warned = True
        return False
