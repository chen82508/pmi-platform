"""Tests for the Slack failure-notification hook (pmi-notify integration).

Mirrors `pmi-ingest/tests/test_health_notify.py`. Covers:
  - `pmi_workers.notify.notify_failure` in isolation: builds and sends a
    filtered-traceback alert (with job/worker/args context), never raises
    even if the notifier itself blows up.
  - `runner.py::_run` (the cron `run-job` CLI path): on the job raising,
    `notify_failure` is called before the existing `sys.exit(1)`.
  - `worker.py::_execute` (the durable Postgres-queue loop): on the job
    raising, `notify_failure` gets the job name/worker id/args AND
    `queue.fail` still runs afterward, unchanged.

No real DB, network, or Slack: `session_scope`/`queue.fail` are patched out,
and the Slack notifier is a local fake implementing `BaseNotificationAdapter`.
"""

from __future__ import annotations

import types
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from pmi_notify import BaseNotificationAdapter, NotificationMessage

from pmi_workers.notify import notify_failure


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


async def test_notify_failure_sends_message_with_job_context() -> None:
    fake = _FakeNotifier()
    exc = ValueError("upstream boom")

    with patch("pmi_workers.notify.get_notifier", return_value=fake):
        await notify_failure(
            job_name="score-all",
            exc=exc,
            worker_id="host-1:123",
            args={"index_id": "war-index-2026"},
        )

    assert len(fake.sent) == 1
    message = fake.sent[0]
    assert message.title == "score-all failed"
    assert "ValueError" in message.body
    assert "upstream boom" in message.body
    assert message.fields["Worker"] == "host-1:123"
    assert "war-index-2026" in message.fields["Args"]
    assert "Environment" in message.fields
    assert "Timestamp (UTC)" in message.fields


async def test_notify_failure_omits_worker_and_defaults_args_when_absent() -> None:
    fake = _FakeNotifier()

    with patch("pmi_workers.notify.get_notifier", return_value=fake):
        await notify_failure(job_name="hourly", exc=ValueError("x"))

    message = fake.sent[0]
    assert "Worker" not in message.fields
    assert message.fields["Args"] == "{}"


async def test_notify_failure_never_raises_when_notifier_itself_fails() -> None:
    fake = _FakeNotifier(raises=RuntimeError("slack sdk internal bug"))

    with patch("pmi_workers.notify.get_notifier", return_value=fake):
        await notify_failure(job_name="hourly", exc=ValueError("original error"))  # must not raise


async def test_notify_failure_never_raises_when_get_notifier_itself_fails() -> None:
    with patch("pmi_workers.notify.get_notifier", side_effect=RuntimeError("factory bug")):
        await notify_failure(job_name="hourly", exc=ValueError("x"))  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# runner.py::_run integration (cron `run-job` CLI path)
# ─────────────────────────────────────────────────────────────────────────────


async def test_run_notifies_and_exits_on_job_failure() -> None:
    from pmi_workers import runner

    async def _failing_job() -> None:
        raise ValueError("job blew up")

    notify_mock = AsyncMock()
    with (
        patch("pmi_workers.runner.registry.get", return_value=_failing_job),
        patch("pmi_workers.runner.notify_failure", notify_mock),
    ):
        with pytest.raises(SystemExit) as exc_info:
            await runner._run("failing-job")

    assert exc_info.value.code == 1
    notify_mock.assert_awaited_once()
    _, kwargs = notify_mock.call_args
    assert kwargs["job_name"] == "failing-job"
    assert isinstance(kwargs["exc"], ValueError)
    assert str(kwargs["exc"]) == "job blew up"


async def test_run_does_not_notify_on_success() -> None:
    from pmi_workers import runner

    async def _ok_job() -> None:
        return None

    notify_mock = AsyncMock()
    with (
        patch("pmi_workers.runner.registry.get", return_value=_ok_job),
        patch("pmi_workers.runner.notify_failure", notify_mock),
    ):
        await runner._run("ok-job")

    notify_mock.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# worker.py::_execute integration (durable Postgres-queue loop)
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _fake_session_scope():
    yield None


async def test_execute_notifies_with_job_context_and_still_fails_job() -> None:
    from pmi_workers import worker

    job = types.SimpleNamespace(
        id=42,
        name="reeval-market",
        attempts=1,
        args={"market_id": 7},
    )

    async def _failing_fn(**kwargs: object) -> None:
        raise ValueError("factor eval blew up")

    notify_mock = AsyncMock()
    fail_mock = AsyncMock()
    with (
        patch("pmi_workers.worker.registry.get", return_value=_failing_fn),
        patch("pmi_workers.worker.session_scope", _fake_session_scope),
        patch("pmi_workers.worker.queue.fail", fail_mock),
        patch("pmi_workers.worker.notify_failure", notify_mock),
        patch("pmi_workers.worker._worker_id", return_value="host-1:99"),
    ):
        await worker._execute(job)

    notify_mock.assert_awaited_once()
    _, kwargs = notify_mock.call_args
    assert kwargs["job_name"] == "reeval-market"
    assert isinstance(kwargs["exc"], ValueError)
    assert kwargs["worker_id"] == "host-1:99"
    assert kwargs["args"] == {"market_id": 7}

    fail_mock.assert_awaited_once()  # notify is additive — queue.fail still runs


async def test_execute_does_not_notify_on_success() -> None:
    from pmi_workers import worker

    job = types.SimpleNamespace(id=1, name="hourly", attempts=1, args=None)

    async def _ok_fn(**kwargs: object) -> dict:
        return {"ok": True}

    notify_mock = AsyncMock()
    complete_mock = AsyncMock()
    with (
        patch("pmi_workers.worker.registry.get", return_value=_ok_fn),
        patch("pmi_workers.worker.session_scope", _fake_session_scope),
        patch("pmi_workers.worker.queue.complete", complete_mock),
        patch("pmi_workers.worker.notify_failure", notify_mock),
    ):
        await worker._execute(job)

    notify_mock.assert_not_awaited()
    complete_mock.assert_awaited_once()
