"""Factory: resolve the right `BaseNotificationAdapter` from configuration.

Reads raw environment variables rather than pydantic-settings, mirroring
`pmi_core.observability.init_sentry` — this package must stay importable (and
usable) from every service, including `pmi-core` itself, without introducing
a settings dependency or an import cycle.

Env vars:
    SLACK_WEBHOOK_URL  - Slack incoming-webhook URL. Empty/unset -> NullNotificationAdapter.
    SLACK_ENVIRONMENT  - tag shown on non-production alerts (default "development").

Adding a new vendor: implement `BaseNotificationAdapter`, add a branch here
keyed off its own env var(s). `decorators.py` never needs to change.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pmi_notify.base import BaseNotificationAdapter, NullNotificationAdapter
from pmi_notify.slack import SlackWebhookAdapter


def build_notifier(
    webhook_url: str | None = None,
    *,
    environment: str | None = None,
) -> BaseNotificationAdapter:
    """Construct an adapter for the given (or environment-derived) config.

    Explicit args win over env vars. A missing/blank webhook URL returns
    `NullNotificationAdapter` rather than raising — callers should never have
    to branch on "is notification configured?" themselves.
    """
    url = (webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "")).strip()
    if not url:
        return NullNotificationAdapter()

    env_tag = environment if environment is not None else os.environ.get("SLACK_ENVIRONMENT", "development")
    return SlackWebhookAdapter(webhook_url=url, environment=env_tag)


@lru_cache(maxsize=1)
def get_notifier() -> BaseNotificationAdapter:
    """Process-wide singleton notifier resolved from the environment.

    Cached so decorators don't build a fresh adapter (and, for Slack, imply a
    fresh `httpx.AsyncClient`) on every decorated call. Tests that flip the
    env vars mid-run must call `get_notifier.cache_clear()` first.
    """
    return build_notifier()
