"""Manual, isolated entrypoint for exercising the Slack webhook from inside
VS Code (Run Python File / F5), without wiring up any other platform service.

Usage:
    export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T000/B000/XXX"
    export SLACK_ENVIRONMENT="staging"        # optional, defaults to "development"
    python -m pmi_notify.run_debug

Without SLACK_WEBHOOK_URL set, every send below no-ops via
NullNotificationAdapter — the script still runs end-to-end so you can verify
the decorator control flow (re-raise, signature preservation) offline.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pmi_notify.decorators import notify_on_failure, notify_on_success
from pmi_notify.factory import build_notifier
from pmi_notify.message import NotificationLevel, NotificationMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pmi_notify.run_debug")


async def _send_raw_message() -> None:
    notifier = build_notifier()
    logger.info("resolved notifier: %s", type(notifier).__name__)
    ok = await notifier.send(
        NotificationMessage(
            title="pmi-notify debug ping",
            body="Manual send triggered from run_debug.py",
            level=NotificationLevel.INFO,
        )
    )
    logger.info("notifier.send() -> %s", ok)


@notify_on_failure(title="run_debug.failing_task", mention="@channel")
async def _failing_task() -> None:
    logger.info("about to raise on purpose to exercise notify_on_failure")
    raise RuntimeError("intentional failure for manual Slack webhook testing")


@notify_on_success(title="run_debug.succeeding_task")
def _succeeding_task() -> str:
    logger.info("running the sync success path")
    return "ok"


def main() -> None:
    if not os.environ.get("SLACK_WEBHOOK_URL"):
        logger.warning(
            "SLACK_WEBHOOK_URL is not set: sends will no-op via NullNotificationAdapter. "
            "Export it (or set it in .env) to see a real message land in Slack."
        )

    logger.info("--- 1/3: raw notifier.send() ---")
    asyncio.run(_send_raw_message())

    logger.info("--- 2/3: @notify_on_success on a sync function ---")
    _succeeding_task()

    logger.info("--- 3/3: @notify_on_failure on an async function (reraise=True) ---")
    try:
        asyncio.run(_failing_task())
    except RuntimeError:
        logger.info("caught the re-raised RuntimeError as expected — decorator did not swallow it")


if __name__ == "__main__":
    main()
