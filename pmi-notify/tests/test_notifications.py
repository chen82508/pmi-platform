"""Unit tests for pmi-notify.

Every test is offline and deterministic: Slack HTTP calls go through
`httpx.MockTransport` (no real network, no `SLACK_WEBHOOK_URL` needed), and
decorator tests inject `FakeNotifier` (see conftest.py) instead of hitting
the environment-resolved singleton — except the handful of factory tests
that specifically exercise env-var resolution.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pmi_notify.base import NullNotificationAdapter
from pmi_notify.decorators import notify_on_failure, notify_on_success
from pmi_notify.factory import build_notifier, get_notifier
from pmi_notify.message import NotificationLevel, NotificationMessage
from pmi_notify.slack import SlackWebhookAdapter
from tests.conftest import FakeNotifier

WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/XXX"


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ─── NullNotificationAdapter ────────────────────────────────────────────────


async def test_null_adapter_returns_false_and_never_raises() -> None:
    adapter = NullNotificationAdapter()
    ok = await adapter.send(NotificationMessage(title="hello"))
    assert ok is False


async def test_null_adapter_warns_only_once(caplog: pytest.LogCaptureFixture) -> None:
    adapter = NullNotificationAdapter()
    await adapter.send(NotificationMessage(title="one"))
    await adapter.send(NotificationMessage(title="two"))
    assert adapter._warned is True  # internal flag flips after first warn


# ─── SlackWebhookAdapter (Adapter pattern) ──────────────────────────────────


async def test_slack_adapter_sends_and_returns_true_on_2xx() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="ok")

    async with _client_with_handler(handler) as client:
        adapter = SlackWebhookAdapter(WEBHOOK_URL, client=client)
        ok = await adapter.send(NotificationMessage(title="pipeline tick", body="5 indexes scored"))

    assert ok is True
    assert len(calls) == 1
    assert calls[0].url == WEBHOOK_URL
    payload = calls[0].content
    assert b"pipeline tick" in payload
    assert b"5 indexes scored" in payload


async def test_slack_adapter_payload_includes_mention_and_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client_with_handler(handler) as client:
        adapter = SlackWebhookAdapter(WEBHOOK_URL, client=client)
        message = NotificationMessage(
            title="war-index-2026 backtest",
            level=NotificationLevel.WARNING,
            mention="@channel",
            fields={"markets": "247", "duration_s": "42"},
        )
        payload = adapter._build_payload(message)

    assert "@channel" in payload["text"]
    block_types = [b["type"] for b in payload["blocks"]]
    assert "context" in block_types  # mention rendered as a context block
    fields_block = next(b for b in payload["blocks"] if b.get("fields"))
    rendered_fields = " ".join(f["text"] for f in fields_block["fields"])
    assert "markets" in rendered_fields and "247" in rendered_fields


async def test_slack_adapter_environment_tag_hidden_in_production() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client_with_handler(handler) as client:
        prod = SlackWebhookAdapter(WEBHOOK_URL, environment="production", client=client)
        staging = SlackWebhookAdapter(WEBHOOK_URL, environment="staging", client=client)
        prod_payload = prod._build_payload(NotificationMessage(title="x"))
        staging_payload = staging._build_payload(NotificationMessage(title="x"))

    assert "[production]" not in prod_payload["text"]
    assert "[staging]" in staging_payload["text"]


async def test_slack_adapter_http_error_returns_false_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    async with _client_with_handler(handler) as client:
        adapter = SlackWebhookAdapter(WEBHOOK_URL, client=client)
        ok = await adapter.send(NotificationMessage(title="will fail"))

    assert ok is False


async def test_slack_adapter_transport_exception_returns_false_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client_with_handler(handler) as client:
        adapter = SlackWebhookAdapter(WEBHOOK_URL, client=client)
        ok = await adapter.send(NotificationMessage(title="network is down"))

    assert ok is False


async def test_slack_adapter_missing_url_still_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never be called for an empty URL")

    async with _client_with_handler(handler) as client:
        adapter = SlackWebhookAdapter("", client=client)
        ok = await adapter.send(NotificationMessage(title="no url"))

    assert ok is False


# ─── Factory ─────────────────────────────────────────────────────────────


def test_build_notifier_returns_null_when_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    notifier = build_notifier()
    assert isinstance(notifier, NullNotificationAdapter)


def test_build_notifier_returns_null_for_blank_url() -> None:
    notifier = build_notifier(webhook_url="   ")
    assert isinstance(notifier, NullNotificationAdapter)


def test_build_notifier_returns_slack_adapter_when_url_present() -> None:
    notifier = build_notifier(webhook_url=WEBHOOK_URL, environment="staging")
    assert isinstance(notifier, SlackWebhookAdapter)
    assert notifier._environment == "staging"


def test_build_notifier_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("SLACK_ENVIRONMENT", "production")
    notifier = build_notifier()
    assert isinstance(notifier, SlackWebhookAdapter)
    assert notifier._environment == "production"


def test_get_notifier_is_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    first = get_notifier()
    second = get_notifier()
    assert first is second


# ─── Decorators: notify_on_failure ──────────────────────────────────────────


async def test_notify_on_failure_async_reraises_and_notifies() -> None:
    fake = FakeNotifier()

    @notify_on_failure(notifier=fake, mention="@channel")
    async def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await boom()

    assert len(fake.sent) == 1
    assert fake.sent[0].level is NotificationLevel.ERROR
    assert "boom failed" in fake.sent[0].title
    assert fake.sent[0].mention == "@channel"


def test_notify_on_failure_sync_reraises_and_notifies() -> None:
    fake = FakeNotifier()

    @notify_on_failure(notifier=fake)
    def boom() -> None:
        raise RuntimeError("sync kaboom")

    with pytest.raises(RuntimeError, match="sync kaboom"):
        boom()

    assert len(fake.sent) == 1
    assert "sync kaboom" in fake.sent[0].body


async def test_notify_on_failure_reraise_false_swallows_exception() -> None:
    fake = FakeNotifier()

    @notify_on_failure(notifier=fake, reraise=False)
    async def boom() -> str:
        raise ValueError("swallowed")

    result = await boom()
    assert result is None
    assert len(fake.sent) == 1


async def test_notify_on_failure_no_alert_on_clean_completion() -> None:
    fake = FakeNotifier()

    @notify_on_failure(notifier=fake)
    async def fine() -> str:
        return "all good"

    result = await fine()
    assert result == "all good"
    assert fake.sent == []


def test_notify_on_failure_never_crashes_when_adapter_itself_raises() -> None:
    fake = FakeNotifier(raises=RuntimeError("slack sdk internal bug"))

    @notify_on_failure(notifier=fake)
    def boom() -> None:
        raise ValueError("original error")

    # The ORIGINAL exception propagates; the adapter's own failure is swallowed.
    with pytest.raises(ValueError, match="original error"):
        boom()


# ─── Decorators: notify_on_success ──────────────────────────────────────────


async def test_notify_on_success_async_notifies_after_success() -> None:
    fake = FakeNotifier()

    @notify_on_success(notifier=fake, title="Nightly backtest")
    async def run() -> int:
        return 42

    result = await run()
    assert result == 42
    assert len(fake.sent) == 1
    assert fake.sent[0].level is NotificationLevel.SUCCESS
    assert fake.sent[0].title == "Nightly backtest succeeded"


def test_notify_on_success_sync_notifies_after_success() -> None:
    fake = FakeNotifier()

    @notify_on_success(notifier=fake)
    def run() -> str:
        return "ok"

    result = run()
    assert result == "ok"
    assert len(fake.sent) == 1


def test_notify_on_success_include_result_embeds_repr() -> None:
    fake = FakeNotifier()

    @notify_on_success(notifier=fake, include_result=True)
    def run() -> dict:
        return {"score": 61.4}

    run()
    assert "61.4" in fake.sent[0].body


async def test_notify_on_success_does_not_fire_on_exception() -> None:
    fake = FakeNotifier()

    @notify_on_success(notifier=fake)
    async def boom() -> None:
        raise ValueError("never notified")

    with pytest.raises(ValueError):
        await boom()

    assert fake.sent == []


# ─── Decorator metadata / signature preservation ────────────────────────────


def test_sync_decorator_preserves_function_metadata() -> None:
    fake = FakeNotifier()

    @notify_on_failure(notifier=fake)
    def my_job(x: int, y: int = 2) -> int:
        """Adds two numbers."""
        return x + y

    assert my_job.__name__ == "my_job"
    assert my_job.__doc__ == "Adds two numbers."
    assert my_job(3) == 5


async def test_async_decorator_preserves_function_metadata() -> None:
    fake = FakeNotifier()

    @notify_on_success(notifier=fake)
    async def my_async_job(x: int) -> int:
        """Doubles a number."""
        return x * 2

    assert my_async_job.__name__ == "my_async_job"
    assert my_async_job.__doc__ == "Doubles a number."
    assert await my_async_job(21) == 42
    assert asyncio.iscoroutinefunction(my_async_job)


def test_decorator_falls_back_to_factory_notifier_when_none_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No explicit `notifier=` + no SLACK_WEBHOOK_URL -> NullNotificationAdapter
    resolved via get_notifier(), and the wrapped function still behaves
    normally (this is the "never crashes the app" contract end-to-end)."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    @notify_on_failure()
    def boom() -> None:
        raise ValueError("unconfigured but still safe")

    with pytest.raises(ValueError, match="unconfigured but still safe"):
        boom()
