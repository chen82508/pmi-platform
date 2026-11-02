"""SNS → Slack bridge for CloudWatch alarms.

Subscribed to the alarm topic. Each SNS record carries a CloudWatch alarm
state-change JSON; it is rendered as a Slack Block Kit message and posted to
the incoming webhook stored in SSM (SecureString). Standard library only, so
the deployment package is just this file.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger()
log.setLevel(logging.INFO)

_STATE_STYLE = {
    "ALARM": (":rotating_light:", "ALARM"),
    "OK": (":white_check_mark:", "RESOLVED"),
    "INSUFFICIENT_DATA": (":grey_question:", "NO DATA"),
}

_webhook_cache: str | None = None


def _webhook_url() -> str:
    """Read the webhook once per container; SSM is only hit on cold start."""
    global _webhook_cache
    if _webhook_cache is None:
        import boto3  # provided by the Lambda runtime

        resp = boto3.client("ssm").get_parameter(
            Name=os.environ["WEBHOOK_PARAMETER"], WithDecryption=True
        )
        _webhook_cache = resp["Parameter"]["Value"]
    return _webhook_cache


def build_message(alarm: dict[str, Any], environment: str) -> dict[str, Any]:
    """Slack payload for one CloudWatch alarm notification."""
    state = alarm.get("NewStateValue", "UNKNOWN")
    emoji, label = _STATE_STYLE.get(state, (":warning:", state))
    name = alarm.get("AlarmName", "unknown alarm")
    trigger = alarm.get("Trigger") or {}
    dims = ", ".join(
        f"{d.get('name')}={d.get('value')}" for d in trigger.get("Dimensions") or []
    )
    fields = [
        f"*Environment*\n{environment}",
        f"*Metric*\n{trigger.get('Namespace', '?')} / {trigger.get('MetricName', '?')}",
        f"*Threshold*\n{trigger.get('ComparisonOperator', '?')} {trigger.get('Threshold', '?')}",
        f"*Since*\n{alarm.get('StateChangeTime', '?')}",
    ]
    if dims:
        fields.append(f"*Dimensions*\n{dims}")
    return {
        "text": f"{emoji} [{label}] {name}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{label}: {name}"[:150]},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} {alarm.get('NewStateReason', '')}"[:3000],
                },
            },
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": f} for f in fields],
            },
        ],
    }


def parse_record(record: dict[str, Any]) -> dict[str, Any]:
    """CloudWatch alarm JSON from an SNS record; plain-text messages are wrapped."""
    message = record["Sns"]["Message"]
    try:
        alarm = json.loads(message)
    except json.JSONDecodeError:
        alarm = None
    if not isinstance(alarm, dict):
        alarm = {
            "AlarmName": record["Sns"].get("Subject") or "SNS notification",
            "NewStateValue": "INFO",
            "NewStateReason": message,
        }
    return alarm


def post(payload: dict[str, Any], url: str) -> int:
    if not url.startswith("https://"):
        raise ValueError("webhook URL must be https")
    req = urllib.request.Request(  # noqa: S310 — scheme checked above
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return resp.status


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, int]:
    environment = os.environ.get("ENVIRONMENT", "unknown")
    url = _webhook_url()
    if not url.startswith("https://"):
        # Parameter still holds the Terraform placeholder: drop, but loudly.
        log.error("slack webhook parameter unset; dropping %d record(s)", len(event["Records"]))
        return {"sent": 0}
    sent = 0
    for record in event["Records"]:
        alarm = parse_record(record)
        post(build_message(alarm, environment), url)
        log.info("posted %s -> %s", alarm.get("AlarmName"), alarm.get("NewStateValue"))
        sent += 1
    return {"sent": sent}
