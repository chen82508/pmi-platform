"""Vendor-neutral notification payload.

`NotificationMessage` is what decorators and call sites build; adapters
(Slack, and whatever ships next) translate it into their own wire format.
Keeping this dumb and dependency-free is what lets `decorators.py` stay
ignorant of Slack entirely (see `base.BaseNotificationAdapter`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class NotificationLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class NotificationMessage:
    title: str
    body: str = ""
    level: NotificationLevel = NotificationLevel.INFO
    mention: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    source: str | None = None
