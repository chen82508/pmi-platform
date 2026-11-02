"""Tests for `python -m pmi_notify send` (pmi_notify/cli.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pmi_notify.base import BaseNotificationAdapter, NullNotificationAdapter
from pmi_notify.cli import EXIT_DELIVERY_FAILED, EXIT_OK, EXIT_UNCONFIGURED, main
from pmi_notify.message import NotificationLevel, NotificationMessage
from tests.conftest import FakeNotifier

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class FlakyNotifier(BaseNotificationAdapter):
    """Fails the first `failures` sends, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def send(self, message: NotificationMessage) -> bool:
        self.calls += 1
        return self.calls > self.failures


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def test_send_delivers_and_returns_zero() -> None:
    fake = FakeNotifier()
    code = main(
        [
            "send",
            "--title",
            "Disk 84%",
            "--body",
            "archive started",
            "--level",
            "warning",
            "--mention",
            "@here",
            "--source",
            "pmi-diskwatch",
            "--field",
            "used=84.2%",
            "--field",
            "free=79 GB",
        ],
        notifier=fake,
    )
    assert code == EXIT_OK
    [msg] = fake.sent
    assert msg.title == "Disk 84%"
    assert msg.body == "archive started"
    assert msg.level is NotificationLevel.WARNING
    assert msg.mention == "@here"
    assert msg.source == "pmi-diskwatch"
    assert msg.fields == {"used": "84.2%", "free": "79 GB"}


@pytest.mark.parametrize(
    ("cli_level", "expected"),
    [
        ("info", NotificationLevel.INFO),
        ("warning", NotificationLevel.WARNING),
        ("critical", NotificationLevel.ERROR),
        ("CRITICAL", NotificationLevel.ERROR),
    ],
)
def test_levels_map_onto_existing_enum(cli_level: str, expected: NotificationLevel) -> None:
    fake = FakeNotifier()
    assert main(["send", "--title", "t", "--level", cli_level], notifier=fake) == EXIT_OK
    assert fake.sent[0].level is expected


def test_retries_with_exponential_backoff_then_succeeds() -> None:
    flaky = FlakyNotifier(failures=2)
    sleep = RecordingSleep()
    code = main(["send", "--title", "t", "--retry-delay", "0.5"], notifier=flaky, sleep=sleep)
    assert code == EXIT_OK
    assert flaky.calls == 3
    assert sleep.delays == [0.5, 1.0]


def test_gives_up_after_three_retries_and_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    flaky = FlakyNotifier(failures=99)
    sleep = RecordingSleep()
    code = main(["send", "--title", "Disk 92%"], notifier=flaky, sleep=sleep)
    assert code == EXIT_DELIVERY_FAILED
    assert flaky.calls == 4  # first attempt + 3 retries
    assert sleep.delays == [1.0, 2.0, 4.0]
    err = capsys.readouterr().err
    assert "delivery failed after 4 attempts" in err
    assert "Traceback" not in err


def test_adapter_exception_is_contained(capsys: pytest.CaptureFixture[str]) -> None:
    boom = FakeNotifier(raises=RuntimeError("webhook exploded"))
    code = main(["send", "--title", "t", "--retries", "1"], notifier=boom, sleep=RecordingSleep())
    assert code == EXIT_DELIVERY_FAILED
    err = capsys.readouterr().err
    assert "RuntimeError: webhook exploded" in err
    assert "Traceback" not in err


def test_unconfigured_webhook_exits_two_without_retrying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleep = RecordingSleep()
    code = main(["send", "--title", "t"], notifier=NullNotificationAdapter(), sleep=sleep)
    assert code == EXIT_UNCONFIGURED
    assert sleep.delays == []
    assert "SLACK_WEBHOOK_URL is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["send"],  # missing --title
        ["send", "--title", "t", "--level", "panic"],
        ["send", "--title", "t", "--field", "no-equals-sign"],
        [],
    ],
)
def test_bad_arguments_exit_two_without_raising(argv: list[str]) -> None:
    assert main(argv, notifier=FakeNotifier()) == EXIT_UNCONFIGURED


def test_module_entry_point_runs_as_subprocess() -> None:
    """`python -m pmi_notify` must exit cleanly (code 2, no traceback) when unconfigured."""
    env = {k: v for k, v in os.environ.items() if k != "SLACK_WEBHOOK_URL"}
    env["PYTHONPATH"] = str(PACKAGE_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "pmi_notify", "send", "--title", "hello"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == EXIT_UNCONFIGURED
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr
