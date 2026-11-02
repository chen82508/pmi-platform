"""weekly-report job — email the "Get our updates" subscribers a weekly digest.

Cron enqueues this Monday 13:00 UTC (see cron/crontab). For each curated
landing index it reads the latest score plus the last score at least 7 days
old (across ALL versions of the index_id, so a version bump mid-week doesn't
blank the delta), renders one shared HTML/plaintext digest, and sends it to
every active `core_subscribers` row with a per-subscriber unsubscribe link
(`{public_web_url}/unsubscribe?token=…`).

Graceful degradation: with PMI_SMTP_HOST unset the job still computes and
logs the digest but skips delivery — same philosophy as the MLflow mirror.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.models import CoreSubscriber
from pmi_core.models.core_subscriber import STATUS_ACTIVE, TOPIC_WEEKLY_REPORT
from sqlalchemy import select, text, update

from pmi_workers.email_links import tagged
from pmi_workers.mailer import OutboundEmail, send_batch, smtp_configured
from pmi_workers.registry import register

log = structlog.get_logger("pmi_workers.jobs.weekly_report")

# (index_id, pmi-web path) — the indexes the digest covers, mirroring the
# landing page's cards. New index → add a line; nothing else changes.
REPORT_INDEXES = [
    ("war-index-2026", "/micah/war"),
    ("us-senate-2026-republican-seats", "/micah/balance-of-power?chamber=senate"),
    ("us-house-2026-republican-seats", "/micah/balance-of-power?chamber=house"),
    ("us-governor-2026-republican-seats", "/micah/balance-of-power?chamber=governor"),
]

def _digest_sql(indexes: list[tuple[str, str]]):
    """Latest + ≥7-day-old score per index_id (across every def version)."""
    return text(
        """
WITH idx(index_id, href) AS (VALUES {values}),
def AS (
    SELECT DISTINCT ON (i.index_id) i.index_id, i.href, d.title
    FROM idx i JOIN core_index_definitions d ON d.index_id = i.index_id
    ORDER BY i.index_id, d.version DESC
)
SELECT def.index_id, def.title, def.href,
       latest.score AS score_now,  latest.as_of AS as_of_now,
       prev.score   AS score_prev, prev.as_of   AS as_of_prev
FROM def
LEFT JOIN LATERAL (
    SELECT ts.score, ts.as_of
    FROM ts_index_scores ts
    JOIN core_index_definitions d2 ON d2.id = ts.index_definition_id
    WHERE d2.index_id = def.index_id AND ts.score IS NOT NULL
    ORDER BY ts.as_of DESC LIMIT 1
) latest ON true
LEFT JOIN LATERAL (
    SELECT ts.score, ts.as_of
    FROM ts_index_scores ts
    JOIN core_index_definitions d2 ON d2.id = ts.index_definition_id
    WHERE d2.index_id = def.index_id AND ts.score IS NOT NULL
      AND ts.as_of <= now() - interval '7 days'
    ORDER BY ts.as_of DESC LIMIT 1
) prev ON true
ORDER BY def.index_id
""".format(
            values=", ".join(f"('{iid}', '{href}')" for iid, href in indexes)
        )
    )


_SQL = _digest_sql(REPORT_INDEXES)

_UNSUB_PLACEHOLDER = "__UNSUBSCRIBE_URL__"


def _fmt_delta(now_v: float | None, prev_v: float | None) -> str:
    if now_v is None or prev_v is None:
        return "—"
    d = now_v - prev_v
    arrow = "▲" if d >= 0 else "▼"
    return f"{arrow} {'+' if d >= 0 else '−'}{abs(d):.1f} this week"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_digest(rows: list[dict], as_of: datetime) -> tuple[str, str, str]:
    """Return (subject, text_template, html_template).

    Both templates carry the ``__UNSUBSCRIBE_URL__`` placeholder that the
    send loop swaps per subscriber.
    """
    base = settings.public_web_url.rstrip("/")
    home = tagged(f"{base}/", "weekly-report")
    subject = f"Micah Weekly Update — {as_of:%B %d, %Y}"

    text_lines = [f"Micah Weekly Update — {as_of:%B %d, %Y}", ""]
    html_rows = []
    for r in rows:
        score = "—" if r["score_now"] is None else f"{r['score_now']:.1f}"
        delta = _fmt_delta(r["score_now"], r["score_prev"])
        url = tagged(f"{base}{r['href']}", "weekly-report")
        text_lines.append(f"{r['title']}: {score} ({delta})  {url}")
        html_rows.append(
            '<tr>'
            f'<td style="padding:10px 0;border-bottom:1px solid #E9E3D5;">'
            f'<a href="{url}" style="color:#1E1A14;text-decoration:none;font-weight:600;">'
            f"{_esc(r['title'])}</a></td>"
            f'<td style="padding:10px 0 10px 16px;border-bottom:1px solid #E9E3D5;'
            f'text-align:right;font-weight:600;white-space:nowrap;">{score}</td>'
            f'<td style="padding:10px 0 10px 16px;border-bottom:1px solid #E9E3D5;'
            f'text-align:right;color:#5C554A;white-space:nowrap;">{_esc(delta)}</td>'
            "</tr>"
        )
    text_lines += [
        "",
        f"Read the indexes: {home}",
        "",
        f"Unsubscribe: {_UNSUB_PLACEHOLDER}",
    ]

    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#FAF7F0;">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;
            font-family:Georgia,'Times New Roman',serif;color:#1E1A14;">
  <h1 style="font-size:22px;font-weight:500;margin:0 0 4px;">Micah Weekly Update</h1>
  <p style="margin:0 0 24px;color:#5C554A;font-size:14px;">
    What real money says happens next — week of {as_of:%B %d, %Y}.</p>
  <table style="width:100%;border-collapse:collapse;font-size:15px;">{''.join(html_rows)}</table>
  <p style="margin:24px 0 0;">
    <a href="{home}" style="display:inline-block;padding:10px 22px;border-radius:999px;
       background:#1E1A14;color:#FAF7F0;text-decoration:none;
       font-family:system-ui,sans-serif;font-size:13px;font-weight:600;">Read the indexes</a></p>
  <p style="margin:32px 0 0;font-size:12px;color:#8A8272;font-family:system-ui,sans-serif;">
    You're getting this because you subscribed at {base}/.
    <a href="{_UNSUB_PLACEHOLDER}" style="color:#8A8272;">Unsubscribe</a></p>
</div>
</body></html>"""
    return subject, "\n".join(text_lines), html


@register("weekly-report", queue="batch")
async def run() -> dict:
    now = datetime.now(UTC)

    async with session_scope() as session:
        score_rows = [dict(r._mapping) for r in (await session.execute(_SQL)).all()]
        subscribers = (
            (
                await session.execute(
                    select(CoreSubscriber).where(
                        CoreSubscriber.status == STATUS_ACTIVE,
                        CoreSubscriber.topic == TOPIC_WEEKLY_REPORT,
                    )
                )
            )
            .scalars()
            .all()
        )
        recipients = [(s.email, s.unsubscribe_token) for s in subscribers]

    scored = [r for r in score_rows if r["score_now"] is not None]
    if not scored:
        log.warning("weekly_report.no_scores", indexes=len(score_rows))
        return {"sent": 0, "subscribers": len(recipients), "skipped": "no_scores"}
    if not recipients:
        log.info("weekly_report.no_subscribers")
        return {"sent": 0, "subscribers": 0, "skipped": "no_subscribers"}

    subject, text_tpl, html_tpl = build_digest(score_rows, now)

    if not smtp_configured():
        log.warning(
            "weekly_report.smtp_unconfigured",
            subscribers=len(recipients),
            subject=subject,
            digest=text_tpl,
        )
        return {
            "sent": 0,
            "subscribers": len(recipients),
            "skipped": "smtp_unconfigured",
        }

    base = settings.public_web_url.rstrip("/")
    specs = [
        OutboundEmail(
            to=email,
            subject=subject,
            text=text_tpl.replace(_UNSUB_PLACEHOLDER, f"{base}/unsubscribe?token={token}"),
            html=html_tpl.replace(_UNSUB_PLACEHOLDER, f"{base}/unsubscribe?token={token}"),
        )
        for email, token in recipients
    ]
    sent = await asyncio.to_thread(send_batch, specs)

    if sent:
        async with session_scope() as session:
            await session.execute(
                update(CoreSubscriber)
                .where(
                    CoreSubscriber.email.in_(sent),
                    CoreSubscriber.topic == TOPIC_WEEKLY_REPORT,
                )
                .values(last_sent_at=now)
            )
            await session.commit()

    log.info(
        "weekly_report.done",
        sent=len(sent),
        subscribers=len(recipients),
        indexes=len(scored),
    )
    return {"sent": len(sent), "subscribers": len(recipients), "indexes": len(scored)}
