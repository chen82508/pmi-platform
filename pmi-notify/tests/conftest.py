"""Shared pytest fixtures/doubles for pmi-notify unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pmi_notify.base import BaseNotificationAdapter
from pmi_notify.factory import get_notifier
from pmi_notify.message import NotificationMessage


class FakeNotifier(BaseNotificationAdapter):
    """In-memory recording adapter used to test decorator orchestration
    without touching the network or the real Slack adapter."""

    def __init__(self, *, result: bool = True, raises: Exception | None = None) -> None:
        self.sent: list[NotificationMessage] = []
        self._result = result
        self._raises = raises

    async def send(self, message: NotificationMessage) -> bool:
        self.sent.append(message)
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.fixture(autouse=True)
def clear_notifier_cache() -> Iterator[None]:
    """`get_notifier()` is an lru_cache singleton — reset it around every test
    so env-var monkeypatches in one test can't leak into another."""
    get_notifier.cache_clear()
    yield
    get_notifier.cache_clear()
