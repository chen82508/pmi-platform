"""notify-new-indexes job tests — detection, announce-once, topic targeting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pmi_core.config import settings
from pmi_core.models import CoreIndexAnnouncement, CoreIndexDefinition, CoreSubscriber
from sqlalchemy import select, text

from pmi_workers.jobs import notify_new_indexes

NOW = datetime.now(UTC)
INDEX_ID = "test-launch-index"


@pytest_asyncio.fixture(autouse=True)
async def _clean_state(session_factory):
    async with session_factory() as s:
        await s.execute(
            text("TRUNCATE core_subscribers, core_index_announcements RESTART IDENTITY")
        )
        # Other test files leave defs behind; mark everything EXCEPT our own
        # launch index as already announced so detection is deterministic.
        await s.execute(
            text(
                "INSERT INTO core_index_announcements (index_id) "
                "SELECT DISTINCT index_id FROM core_index_definitions "
                f"WHERE index_id <> '{INDEX_ID}'"
            )
        )
        await s.commit()


@pytest.fixture(autouse=True)
def _smtp_off(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "public_web_url", "http://web.test")


@pytest_asyncio.fixture
async def launched_index(session_factory):
    async with session_factory() as s:
        existing = (
            await s.execute(
                select(CoreIndexDefinition).where(
                    CoreIndexDefinition.index_id == INDEX_ID
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                CoreIndexDefinition(
                    index_id=INDEX_ID,
                    version=1,
                    title="Test Launch Index",
                    owner="test",
                    definition={"id": INDEX_ID, "version": 1},
                    yaml_source=f"id: {INDEX_ID}\nversion: 1\n",
                    yaml_sha256="b" * 64,
                    is_current=True,
                    effective_from=NOW - timedelta(hours=1),
                )
            )
            await s.commit()


async def _add_subscriber(session_factory, email: str, topic: str) -> str:
    token = f"tok-{topic}-{email.split('@')[0]}"
    async with session_factory() as s:
        s.add(
            CoreSubscriber(
                email=email, topic=topic, status="active", unsubscribe_token=token
            )
        )
        await s.commit()
    return token


async def test_nothing_new_is_a_noop(session_factory):
    result = await notify_new_indexes.run()
    assert result == {"announced": 0, "sent": 0}


async def test_announces_once_even_without_subscribers(launched_index, session_factory):
    result = await notify_new_indexes.run()
    assert result == {"announced": 1, "sent": 0}

    async with session_factory() as s:
        row = (
            await s.execute(
                select(CoreIndexAnnouncement).where(
                    CoreIndexAnnouncement.index_id == INDEX_ID
                )
            )
        ).scalar_one()
        assert row.notified_subscribers == 0

    # Second run: already announced → clean no-op.
    assert await notify_new_indexes.run() == {"announced": 0, "sent": 0}


async def test_emails_new_index_subscribers_only(
    launched_index, session_factory, monkeypatch
):
    token = await _add_subscriber(session_factory, "launch@example.com", "new-index")
    await _add_subscriber(session_factory, "weekly@example.com", "weekly-report")

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    captured: list = []

    def fake_send_batch(specs):
        captured.extend(specs)
        return [s.to for s in specs]

    monkeypatch.setattr(notify_new_indexes, "send_batch", fake_send_batch)

    result = await notify_new_indexes.run()
    assert result == {"announced": 1, "sent": 1}

    assert [s.to for s in captured] == ["launch@example.com"]
    spec = captured[0]
    assert "New on Micah: Test Launch Index" == spec.subject
    assert "Test Launch Index" in spec.text and "Test Launch Index" in spec.html
    unsub = f"http://web.test/unsubscribe?token={token}"
    assert unsub in spec.text and unsub in spec.html
    assert "__UNSUBSCRIBE_URL__" not in spec.text

    async with session_factory() as s:
        ann = (
            await s.execute(
                select(CoreIndexAnnouncement).where(
                    CoreIndexAnnouncement.index_id == INDEX_ID
                )
            )
        ).scalar_one()
        assert ann.notified_subscribers == 1
        launch_sub = (
            await s.execute(
                select(CoreSubscriber).where(
                    CoreSubscriber.email == "launch@example.com"
                )
            )
        ).scalar_one()
        assert launch_sub.last_sent_at is not None
        weekly_sub = (
            await s.execute(
                select(CoreSubscriber).where(
                    CoreSubscriber.email == "weekly@example.com"
                )
            )
        ).scalar_one()
        assert weekly_sub.last_sent_at is None
