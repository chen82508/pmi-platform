"""Adapter: Slack incoming-webhook transport.

Ported from the legacy `micah-job-executor` `SlackService`
(`app/shared/services/slack.py`) — same no-op-when-unconfigured and
never-crash-the-caller behaviour, rewritten to:
  * implement `BaseNotificationAdapter` (async-first, DIP-compliant)
  * accept an injectable `httpx.AsyncClient` (so tests never touch the network)
  * map the vendor-neutral `NotificationMessage` to Slack Block Kit instead of
    a bare `text` string, and tag messages with an environment label so
    staging/production alerts aren't visually ambiguous
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from pmi_notify.base import BaseNotificationAdapter
from pmi_notify.message import NotificationLevel, NotificationMessage

log = structlog.get_logger(__name__)

_LEVEL_EMOJI: dict[NotificationLevel, str] = {
    NotificationLevel.INFO: "ℹ️",
    NotificationLevel.SUCCESS: "✅",
    NotificationLevel.WARNING: "⚠️",
    NotificationLevel.ERROR: "\U0001f6a8",
}


class SlackWebhookAdapter(BaseNotificationAdapter):
    """Sends `NotificationMessage`s to a single Slack incoming webhook.

    A single incoming-webhook URL is bound to exactly one Slack channel by
    Slack's own design — there is no way to route to an arbitrary channel
    per-call without a bot token + `chat.postMessage`. Multi-channel routing
    is therefore out of scope for this adapter; a future
    `SlackBotTokenAdapter` could support it behind the same interface.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        environment: str = "development",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._environment = environment
        self._timeout = timeout
        self._client = client  # injected in tests; otherwise one-shot per send()

    async def send(self, message: NotificationMessage) -> bool:
        payload = self._build_payload(message)
        try:
            if self._client is not None:
                resp = await self._client.post(self._webhook_url, json=payload, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - transport failures must never propagate
            log.warning("notify.slack_send_failed", error=str(exc), title=message.title)
            return False
        return True

    def _build_payload(self, message: NotificationMessage) -> dict[str, Any]:
        emoji = _LEVEL_EMOJI.get(message.level, "")
        env_tag = f"[{self._environment}] " if self._environment != "production" else ""
        header = f"{emoji} {env_tag}{message.title}".strip()

        text_lines = [header]
        if message.mention:
            text_lines.append(message.mention)
        if message.body:
            text_lines.append(message.body)

        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        if message.body:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": message.body}})
        if message.fields:
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{key}*\n{value}"}
                        for key, value in message.fields.items()
                    ],
                }
            )
        if message.mention:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": message.mention}]})

        return {"text": "\n".join(text_lines), "blocks": blocks}
