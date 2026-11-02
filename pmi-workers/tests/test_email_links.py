"""Every link in our own emails carries the newsletter UTM tags (no DB needed)."""

from __future__ import annotations

from datetime import UTC, datetime

from pmi_core.config import settings

from pmi_workers.email_links import tagged
from pmi_workers.jobs.notify_new_indexes import build_announcement
from pmi_workers.jobs.weekly_report import build_digest

WEEKLY = "utm_source=newsletter&utm_medium=email&utm_campaign=weekly-report"
LAUNCH = "utm_source=newsletter&utm_medium=email&utm_campaign=new-index"


def test_tagged_appends_the_newsletter_utm():
    assert tagged("http://web.test/micah/war", "weekly-report") == f"http://web.test/micah/war?{WEEKLY}"
    # An existing query keeps its own parameters first.
    assert tagged("http://web.test/micah/markets?index=sp500", "new-index") == (
        f"http://web.test/micah/markets?index=sp500&{LAUNCH}"
    )


def test_weekly_digest_links_are_tagged_except_unsubscribe(monkeypatch):
    monkeypatch.setattr(settings, "public_web_url", "http://web.test")
    rows = [
        {"index_id": "war-index-2026", "title": "Global War Index", "href": "/micah/war", "score_now": 16.3, "score_prev": 15.9},
    ]
    _subject, text, html = build_digest(rows, datetime(2026, 10, 6, tzinfo=UTC))
    assert f"http://web.test/micah/war?{WEEKLY}" in text
    assert f"Read the indexes: http://web.test/?{WEEKLY}" in text
    assert f'href="http://web.test/micah/war?{WEEKLY}"' in html
    assert f'href="http://web.test/?{WEEKLY}"' in html
    # The unsubscribe placeholder is swapped per subscriber later and stays untagged.
    assert 'href="__UNSUBSCRIBE_URL__"' in html
    assert "Unsubscribe: __UNSUBSCRIBE_URL__" in text


def test_launch_announcement_links_are_tagged_except_unsubscribe(monkeypatch):
    monkeypatch.setattr(settings, "public_web_url", "http://web.test")
    _subject, text, html = build_announcement([{"index_id": "x", "title": "New Index"}])
    assert f"See it live: http://web.test/?{LAUNCH}" in text
    assert f'href="http://web.test/?{LAUNCH}"' in html
    assert 'href="__UNSUBSCRIBE_URL__"' in html
    assert "Unsubscribe: __UNSUBSCRIBE_URL__" in text
