"""pmi-notify: vendor-neutral, best-effort notification decorators.

Structure:
  - `message.py`           NotificationMessage / NotificationLevel — the vendor-neutral payload
  - `base.py`               BaseNotificationAdapter (ABC) + NullNotificationAdapter — the DIP seam
  - `slack.py`              SlackWebhookAdapter — the Adapter (wraps httpx + Slack Block Kit)
  - `factory.py`            build_notifier() / get_notifier() — the Factory
  - `traceback_filter.py`   filter_traceback() — project-native-only stack trace rendering
  - `decorators.py`         notify_on_failure / notify_on_success / build_failure_message

Adding a new vendor (Teams, Discord, PagerDuty, ...):
    1. Implement `BaseNotificationAdapter` in a new module.
    2. Add a branch for it in `factory.build_notifier`.
    3. No decorator changes required (Open-Closed).
"""

from pmi_notify.base import BaseNotificationAdapter, NullNotificationAdapter
from pmi_notify.decorators import build_failure_message, notify_on_failure, notify_on_success
from pmi_notify.factory import build_notifier, get_notifier
from pmi_notify.message import NotificationLevel, NotificationMessage
from pmi_notify.slack import SlackWebhookAdapter
from pmi_notify.traceback_filter import DEFAULT_EXCLUDE_MARKERS, filter_traceback

__all__ = [
    "BaseNotificationAdapter",
    "NullNotificationAdapter",
    "SlackWebhookAdapter",
    "NotificationLevel",
    "NotificationMessage",
    "DEFAULT_EXCLUDE_MARKERS",
    "build_failure_message",
    "build_notifier",
    "filter_traceback",
    "get_notifier",
    "notify_on_failure",
    "notify_on_success",
]
