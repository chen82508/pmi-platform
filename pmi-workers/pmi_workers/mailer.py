"""Minimal SMTP mailer for the weekly-report job.

Deliberately stdlib-only (`smtplib` + `email.message`) — no new dependency
for one batch send a week. The blocking SMTP session is expected to run under
``asyncio.to_thread`` from the async job.

Config comes from ``pmi_core.config.settings`` (PMI_SMTP_* env vars). An empty
``smtp_host`` means "email delivery disabled": callers should check
``smtp_configured()`` and degrade gracefully instead of erroring.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import structlog
from pmi_core.config import settings

log = structlog.get_logger("pmi_workers.mailer")


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    text: str
    html: str


def smtp_configured() -> bool:
    return bool(settings.smtp_host)


def _build_message(spec: OutboundEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = spec.to
    msg["Subject"] = spec.subject
    msg.set_content(spec.text)
    msg.add_alternative(spec.html, subtype="html")
    return msg


def send_batch(specs: list[OutboundEmail]) -> list[str]:
    """Send every spec over ONE SMTP session; return addresses handed off.

    Per-recipient failures (bad mailbox, transient 4xx) are logged and
    skipped so one bad address never aborts the rest of the batch. A
    connection-level failure raises — the job's retry/backoff handles it.
    """
    if not specs:
        return []

    sent: list[str] = []
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        for spec in specs:
            try:
                smtp.send_message(_build_message(spec))
                sent.append(spec.to)
            except smtplib.SMTPException as exc:
                log.warning("mailer.send_failed", to=spec.to, error=str(exc))
    return sent
