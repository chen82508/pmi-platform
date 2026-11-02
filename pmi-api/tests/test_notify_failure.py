"""Tests for the Slack failure-notification hook (pmi-notify integration).

Mirrors `pmi-ingest/tests/test_health_notify.py` / `pmi-workers/tests/
test_notify_failure.py`. Covers:
  - `pmi_api.notify.notify_failure` in isolation: builds and sends a
    filtered-traceback alert (with route/method/request-id context), never
    raises even if the notifier itself blows up.
  - `main.py`'s catch-all `@app.exception_handler(Exception)`: an unhandled
    exception in a route calls `notify_failure` with the route path, then
    still returns an opaque 500 body instead of propagating.

No real Slack: the notifier is a local fake implementing
`BaseNotificationAdapter`. The exception-handler test reuses the existing
`client` fixture (already wired to the ephemeral test DB) and registers a
throwaway route rather than seeding new fixture data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from pmi_notify import BaseNotificationAdapter, NotificationMessage

from pmi_api.notify import notify_failure


class _FakeNotifier(BaseNotificationAdapter):
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.sent: list[NotificationMessage] = []
        self._raises = raises

    async def send(self, message: NotificationMessage) -> bool:
        self.sent.append(message)
        if self._raises is not None:
            raise self._raises
        return True


# ─────────────────────────────────────────────────────────────────────────────
# notify_failure() in isolation
# ─────────────────────────────────────────────────────────────────────────────


async def test_notify_failure_sends_message_with_request_context() -> None:
    fake = _FakeNotifier()
    exc = ValueError("db blew up")

    with patch("pmi_api.notify.get_notifier", return_value=fake):
        await notify_failure(
            route="/indexes/war-index-2026/score",
            exc=exc,
            method="GET",
            request_id="req-123",
        )

    assert len(fake.sent) == 1
    message = fake.sent[0]
    assert message.title == "/indexes/war-index-2026/score failed"
    assert "ValueError" in message.body
    assert "db blew up" in message.body
    assert message.fields["Method"] == "GET"
    assert message.fields["Request ID"] == "req-123"
    assert "Environment" in message.fields
    assert "Timestamp (UTC)" in message.fields


async def test_notify_failure_omits_optional_fields_when_absent() -> None:
    fake = _FakeNotifier()

    with patch("pmi_api.notify.get_notifier", return_value=fake):
        await notify_failure(route="/health", exc=ValueError("x"))

    message = fake.sent[0]
    assert "Method" not in message.fields
    assert "Request ID" not in message.fields


async def test_notify_failure_never_raises_when_notifier_itself_fails() -> None:
    fake = _FakeNotifier(raises=RuntimeError("slack sdk internal bug"))

    with patch("pmi_api.notify.get_notifier", return_value=fake):
        await notify_failure(route="/health", exc=ValueError("original error"))  # must not raise


async def test_notify_failure_never_raises_when_get_notifier_itself_fails() -> None:
    with patch("pmi_api.notify.get_notifier", side_effect=RuntimeError("factory bug")):
        await notify_failure(route="/health", exc=ValueError("x"))  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# main.py catch-all exception handler integration
# ─────────────────────────────────────────────────────────────────────────────
#
# Starlette's ServerErrorMiddleware always re-raises the original exception
# after handing it to a registered `Exception` handler — that's how a real
# ASGI server (uvicorn) still gets to log it, even though the client already
# received our custom 500 body. httpx's ASGITransport mirrors that re-raise
# by default (`raise_app_exceptions=True`) so tests see the true traceback;
# these two tests need `raise_app_exceptions=False` instead, since they're
# specifically asserting on the response our handler builds.


async def test_unhandled_exception_notifies_and_returns_opaque_500(
    client: AsyncClient,
) -> None:
    from pmi_api.main import app

    async def _boom() -> None:
        raise ValueError("route blew up")

    app.add_api_route("/__test_boom", _boom, methods=["GET"])
    try:
        notify_mock = AsyncMock()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch("pmi_api.main.notify_failure", notify_mock):
                response = await ac.get("/__test_boom")
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_boom"
        ]

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "request_id" in body["error"]

    notify_mock.assert_awaited_once()
    _, kwargs = notify_mock.call_args
    assert kwargs["route"] == "/__test_boom"
    assert kwargs["method"] == "GET"
    assert isinstance(kwargs["exc"], ValueError)
    assert str(kwargs["exc"]) == "route blew up"
    assert kwargs["request_id"] == body["error"]["request_id"]


async def test_unhandled_exception_echoes_caller_request_id(
    client: AsyncClient,
) -> None:
    from pmi_api.main import app

    async def _boom() -> None:
        raise ValueError("route blew up")

    app.add_api_route("/__test_boom_2", _boom, methods=["GET"])
    try:
        notify_mock = AsyncMock()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch("pmi_api.main.notify_failure", notify_mock):
                response = await ac.get(
                    "/__test_boom_2", headers={"X-Request-Id": "caller-supplied-id"}
                )
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_boom_2"
        ]

    assert response.json()["error"]["request_id"] == "caller-supplied-id"
    _, kwargs = notify_mock.call_args
    assert kwargs["request_id"] == "caller-supplied-id"


async def test_healthy_route_does_not_notify(client: AsyncClient) -> None:
    notify_mock = AsyncMock()
    with patch("pmi_api.main.notify_failure", notify_mock):
        response = await client.get("/health")

    assert response.status_code == 200
    notify_mock.assert_not_awaited()
