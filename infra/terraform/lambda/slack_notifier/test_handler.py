from __future__ import annotations

import json
from typing import Any

import pytest

import handler

ALARM = {
    "AlarmName": "pmi-platform-disk-high",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold Crossed: 1 datapoint [91.2] > the threshold (85.0).",
    "StateChangeTime": "2026-11-02T10:00:00.000+0000",
    "Trigger": {
        "MetricName": "disk_used_percent",
        "Namespace": "pmi-platform/host",
        "ComparisonOperator": "GreaterThanThreshold",
        "Threshold": 85.0,
        "Dimensions": [{"name": "InstanceId", "value": "i-0123456789abcdef0"}],
    },
}


def _event(message: str, subject: str | None = None) -> dict[str, Any]:
    return {"Records": [{"Sns": {"Message": message, "Subject": subject}}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setattr(handler, "_webhook_cache", None)


def test_alarm_message_carries_state_metric_and_dimensions() -> None:
    msg = handler.build_message(ALARM, "prod")
    assert msg["text"] == ":rotating_light: [ALARM] pmi-platform-disk-high"
    fields = " ".join(f["text"] for f in msg["blocks"][2]["fields"])
    assert "pmi-platform/host / disk_used_percent" in fields
    assert "GreaterThanThreshold 85.0" in fields
    assert "InstanceId=i-0123456789abcdef0" in fields
    assert "prod" in fields


def test_ok_transition_reads_as_resolved() -> None:
    msg = handler.build_message({**ALARM, "NewStateValue": "OK"}, "prod")
    assert msg["text"].startswith(":white_check_mark: [RESOLVED]")


def test_plain_text_sns_message_is_wrapped() -> None:
    alarm = handler.parse_record(_event("hello", subject="Test")["Records"][0])
    assert alarm == {"AlarmName": "Test", "NewStateValue": "INFO", "NewStateReason": "hello"}


def test_handler_posts_one_message_per_record(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[dict, str]] = []
    monkeypatch.setattr(handler, "_webhook_url", lambda: "https://hooks.slack.test/x")
    monkeypatch.setattr(handler, "post", lambda payload, url: posted.append((payload, url)) or 200)

    event = _event(json.dumps(ALARM))
    event["Records"].append(event["Records"][0])
    assert handler.lambda_handler(event, None) == {"sent": 2}
    assert [url for _, url in posted] == ["https://hooks.slack.test/x"] * 2
    assert "environment" not in posted[0][0]  # payload is the Slack body only


def test_placeholder_webhook_drops_without_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_webhook_url", lambda: "UNSET")
    monkeypatch.setattr(handler, "post", lambda *a: pytest.fail("must not post"))
    assert handler.lambda_handler(_event(json.dumps(ALARM)), None) == {"sent": 0}
