"""notify-new-indexes job — email the "new-index" subscribers on index launches.

Cron enqueues this hourly. Detection = current index definitions whose
index_id has no `core_index_announcements` row (the 0019 migration backfilled
the pre-existing catalogue, so only genuinely new launches surface). Every
detected index is marked announced in the same run whether or not any email
went out — an index launch is announced at most once; there is no backlog
that could dump weeks of "new" indexes on the first subscriber later.

Graceful degradation mirrors weekly-report: with PMI_SMTP_HOST unset the job
logs the announcement and records it without sending.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.models import CoreIndexAnnouncement, CoreSubscriber
from pmi_core.models.core_subscriber import STATUS_ACTIVE, TOPIC_NEW_INDEX
from sqlalchemy import select, text, update

from pmi_workers.email_links import tagged
from pmi_workers.mailer import OutboundEmail, send_batch, smtp_configured
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.notify_new_indexes")

_NEW_INDEXES_SQL = text(
    """
SELECT DISTINCT ON (d.index_id) d.index_id, d.title
FROM core_index_definitions d
WHERE d.is_current = true
  AND NOT EXISTS (
      SELECT 1 FROM core_index_announcements a WHERE a.index_id = d.index_id
  )
ORDER BY d.index_id, d.version DESC
"""
)

_UNSUB_PLACEHOLDER = "__UNSUBSCRIBE_URL__"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_announcement(new_indexes: list[dict]) -> tuple[str, str, str]:
    """Return (subject, text_template, html_template) for one launch email.

    One email covers every index detected in this run. Both templates carry
    the ``__UNSUBSCRIBE_URL__`` placeholder swapped per subscriber.
    """
    base = settings.public_web_url.rstrip("/")
    home = tagged(f"{base}/", "new-index")
    titles = [r["title"] for r in new_indexes]
    subject = (
        f"New on Micah: {titles[0]}"
        if len(titles) == 1
        else f"New on Micah: {len(titles)} new indexes"
    )

    text_lines = ["A new index just went live on Micah:", ""]
    html_items = []
    for t in titles:
        text_lines.append(f"  • {t}")
        html_items.append(
            f'<li style="margin:0 0 8px;font-weight:600;">{_esc(t)}</li>'
        )
    text_lines += [
        "",
        f"See it live: {home}",
        "",
        f"Unsubscribe: {_UNSUB_PLACEHOLDER}",
    ]

    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#FAF7F0;">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;
            font-family:Georgia,'Times New Roman',serif;color:#1E1A14;">
  <h1 style="font-size:22px;font-weight:500;margin:0 0 4px;">New on Micah</h1>
  <p style="margin:0 0 20px;color:#5C554A;font-size:14px;">
    {"A new index just went live." if len(titles) == 1 else f"{len(titles)} new indexes just went live."}</p>
  <ul style="margin:0 0 24px;padding-left:20px;font-size:16px;">{''.join(html_items)}</ul>
  <p style="margin:0;">
    <a href="{home}" style="display:inline-block;padding:10px 22px;border-radius:999px;
       background:#1E1A14;color:#FAF7F0;text-decoration:none;
       font-family:system-ui,sans-serif;font-size:13px;font-weight:600;">See it live</a></p>
  <p style="margin:32px 0 0;font-size:12px;color:#8A8272;font-family:system-ui,sans-serif;">
    You're getting this because you asked to hear about new indexes at {base}/.
    <a href="{_UNSUB_PLACEHOLDER}" style="color:#8A8272;">Unsubscribe</a></p>
</div>
</body></html>"""
    return subject, "\n".join(text_lines), html


@register("notify-new-indexes")
async def run() -> dict:
    now = datetime.now(UTC)

    async with session_scope() as session:
        new_indexes = [
            dict(r._mapping)
            for r in (await session.execute(_NEW_INDEXES_SQL)).all()
        ]
        subscribers = (
            (
                await session.execute(
                    select(CoreSubscriber).where(
                        CoreSubscriber.status == STATUS_ACTIVE,
                        CoreSubscriber.topic == TOPIC_NEW_INDEX,
                    )
                )
            )
            .scalars()
            .all()
        )
        recipients = [(s.email, s.unsubscribe_token) for s in subscribers]

    if not new_indexes:
        return {"announced": 0, "sent": 0}

    subject, text_tpl, html_tpl = build_announcement(new_indexes)

    sent: list[str] = []
    if not recipients:
        log.info(
            "notify_new_indexes.no_subscribers",
            indexes=[r["index_id"] for r in new_indexes],
        )
    elif not smtp_configured():
        log.warning(
            "notify_new_indexes.smtp_unconfigured",
            subscribers=len(recipients),
            subject=subject,
            announcement=text_tpl,
        )
    else:
        base = settings.public_web_url.rstrip("/")
        specs = [
            OutboundEmail(
                to=email,
                subject=subject,
                text=text_tpl.replace(
                    _UNSUB_PLACEHOLDER, f"{base}/unsubscribe?token={token}"
                ),
                html=html_tpl.replace(
                    _UNSUB_PLACEHOLDER, f"{base}/unsubscribe?token={token}"
                ),
            )
            for email, token in recipients
        ]
        sent = await asyncio.to_thread(send_batch, specs)

    # Mark announced regardless of delivery outcome — a launch is announced
    # at most once, never queued up for a later subscriber.
    async with session_scope() as session:
        session.add_all(
            CoreIndexAnnouncement(
                index_id=r["index_id"],
                announced_at=now,
                notified_subscribers=len(sent),
            )
            for r in new_indexes
        )
        if sent:
            await session.execute(
                update(CoreSubscriber)
                .where(
                    CoreSubscriber.email.in_(sent),
                    CoreSubscriber.topic == TOPIC_NEW_INDEX,
                )
                .values(last_sent_at=now)
            )
        await session.commit()

    log.info(
        "notify_new_indexes.done",
        announced=[r["index_id"] for r in new_indexes],
        sent=len(sent),
        subscribers=len(recipients),
    )
    return {"announced": len(new_indexes), "sent": len(sent)}
