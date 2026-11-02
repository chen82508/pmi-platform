"""`python -m pmi_notify send …` — send one notification from a shell.

For callers that are not Python services with pmi-notify on their import path,
chiefly the host-level disk watcher (deploy/diskwatch/), which runs it through
`docker exec` in a container that already mounts this package. Uses the same
factory and Slack adapter as the services, so there is one message format and
one SLACK_WEBHOOK_URL.

Fail-safe by design: it never raises and never prints a traceback. A delivery
failure is retried, then reported as one line on stderr plus an exit code:

    0  delivered
    1  delivery failed after all retries (caller may fall back)
    2  not configured (SLACK_WEBHOOK_URL unset) or bad arguments
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from pmi_notify.base import BaseNotificationAdapter, NullNotificationAdapter
from pmi_notify.factory import build_notifier
from pmi_notify.message import NotificationLevel, NotificationMessage

EXIT_OK = 0
EXIT_DELIVERY_FAILED = 1
EXIT_UNCONFIGURED = 2

# The CLI's severity vocabulary. "critical" maps onto the existing ERROR level
# (🚨) so the shared enum, used by every service, stays unchanged.
LEVELS: dict[str, NotificationLevel] = {
    "info": NotificationLevel.INFO,
    "success": NotificationLevel.SUCCESS,
    "warning": NotificationLevel.WARNING,
    "critical": NotificationLevel.ERROR,
}


def _parse_field(raw: str) -> tuple[str, str]:
    key, sep, value = raw.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {raw!r}")
    return key.strip(), value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pmi_notify", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="send one notification")
    send.add_argument("--title", required=True)
    send.add_argument("--body", default="", help="message text (Slack mrkdwn)")
    send.add_argument("--level", choices=sorted(LEVELS), default="info", type=str.lower)
    send.add_argument("--mention", default=None, help="e.g. @here or @channel")
    send.add_argument("--source", default=None, help="who sent it, e.g. pmi-diskwatch")
    send.add_argument(
        "--field",
        dest="fields",
        action="append",
        default=[],
        type=_parse_field,
        metavar="KEY=VALUE",
        help="extra key/value shown in the message (repeatable)",
    )
    send.add_argument(
        "--retries", type=int, default=3, help="retries after the first attempt (default 3)"
    )
    send.add_argument(
        "--retry-delay",
        type=float,
        default=1.0,
        help="first backoff in seconds; doubles each retry",
    )
    return parser


def send_with_retry(
    notifier: BaseNotificationAdapter,
    message: NotificationMessage,
    *,
    retries: int,
    retry_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Deliver with up to `retries` retries and exponential backoff. Never raises."""
    attempts = 1 + max(retries, 0)
    for attempt in range(attempts):
        try:
            if notifier.send_sync(message):
                return True
        except Exception as exc:  # noqa: BLE001 - adapters shouldn't raise, but never trust that here
            print(
                f"pmi_notify: attempt {attempt + 1} raised {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        if attempt < attempts - 1:
            sleep(retry_delay * (2**attempt))
    return False


def main(
    argv: Sequence[str] | None = None,
    *,
    notifier: BaseNotificationAdapter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage to stderr
        return EXIT_OK if exc.code == 0 else EXIT_UNCONFIGURED

    notifier = notifier if notifier is not None else build_notifier()
    if isinstance(notifier, NullNotificationAdapter):
        print("pmi_notify: SLACK_WEBHOOK_URL is not set; nothing sent", file=sys.stderr)
        return EXIT_UNCONFIGURED

    message = NotificationMessage(
        title=args.title,
        body=args.body,
        level=LEVELS[args.level],
        mention=args.mention,
        fields=dict(args.fields),
        source=args.source,
    )
    if send_with_retry(
        notifier, message, retries=args.retries, retry_delay=args.retry_delay, sleep=sleep
    ):
        return EXIT_OK
    print(
        f"pmi_notify: delivery failed after {1 + max(args.retries, 0)} attempts: {args.title!r}",
        file=sys.stderr,
    )
    return EXIT_DELIVERY_FAILED
