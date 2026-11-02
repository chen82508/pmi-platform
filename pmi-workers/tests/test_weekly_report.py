"""weekly-report job tests — digest content, delivery, graceful degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pmi_core.config import settings
from pmi_core.models import CoreIndexDefinition, CoreSubscriber, TsIndexScore
from sqlalchemy import select, text

from pmi_workers.jobs import weekly_report

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(autouse=True)
async def _clean_subscribers(session_factory):
    async with session_factory() as s:
        await s.execute(text("TRUNCATE core_subscribers RESTART IDENTITY"))
        await s.commit()


@pytest.fixture(autouse=True)
def _smtp_off(monkeypatch):
    """Default every test to 'SMTP unconfigured' regardless of host .env."""
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "public_web_url", "http://web.test")


@pytest_asyncio.fixture
async def seeded_scores(session_factory):
    """war-index-2026 with a fresh score (60.0) and an 8-day-old one (55.0)."""
    async with session_factory() as s:
        existing = (
            await s.execute(
                select(CoreIndexDefinition).where(
                    CoreIndexDefinition.index_id == "war-index-2026"
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        d = CoreIndexDefinition(
            index_id="war-index-2026",
            version=1,
            title="War Index 2026",
            owner="test",
            definition={"id": "war-index-2026", "version": 1},
            yaml_source="id: war-index-2026\nversion: 1\n",
            yaml_sha256="a" * 64,
            is_current=True,
            effective_from=NOW - timedelta(days=30),
        )
        s.add(d)
        await s.flush()
        s.add_all(
            [
                TsIndexScore(
                    index_definition_id=d.id,
                    as_of=NOW - timedelta(hours=1),
                    score=60.0,
                    component_count=3,
                    component_evaluation_ids=[],
                ),
                TsIndexScore(
                    index_definition_id=d.id,
                    as_of=NOW - timedelta(days=8),
                    score=55.0,
                    component_count=3,
                    component_evaluation_ids=[],
                ),
            ]
        )
        await s.commit()


async def _add_subscriber(
    session_factory,
    email: str,
    status: str = "active",
    topic: str = "weekly-report",
) -> str:
    token = f"tok-{topic}-{email.split('@')[0]}"
    async with session_factory() as s:
        s.add(
            CoreSubscriber(
                email=email,
                topic=topic,
                status=status,
                unsubscribe_token=token,
                unsubscribed_at=NOW if status == "unsubscribed" else None,
            )
        )
        await s.commit()
    return token


async def test_no_scores_skips(monkeypatch, session_factory):
    await _add_subscriber(session_factory, "a@example.com")
    monkeypatch.setattr(
        weekly_report, "_SQL", weekly_report._digest_sql([("no-such-index", "/x")])
    )
    result = await weekly_report.run()
    assert result == {"sent": 0, "subscribers": 1, "skipped": "no_scores"}


async def test_no_subscribers_skips(seeded_scores):
    result = await weekly_report.run()
    assert result == {"sent": 0, "subscribers": 0, "skipped": "no_subscribers"}


async def test_smtp_unconfigured_skips_delivery(seeded_scores, session_factory):
    await _add_subscriber(session_factory, "a@example.com")
    result = await weekly_report.run()
    assert result == {"sent": 0, "subscribers": 1, "skipped": "smtp_unconfigured"}


async def test_sends_personalised_digest_and_stamps(
    seeded_scores, session_factory, monkeypatch
):
    token_a = await _add_subscriber(session_factory, "a@example.com")
    await _add_subscriber(session_factory, "gone@example.com", status="unsubscribed")
    # A new-index subscriber must NOT receive the weekly digest.
    await _add_subscriber(session_factory, "launches@example.com", topic="new-index")

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    captured: list = []

    def fake_send_batch(specs):
        captured.extend(specs)
        return [s.to for s in specs]

    monkeypatch.setattr(weekly_report, "send_batch", fake_send_batch)

    result = await weekly_report.run()
    assert result == {"sent": 1, "subscribers": 1, "indexes": 1}

    # Only the active subscriber got an email.
    assert [s.to for s in captured] == ["a@example.com"]
    spec = captured[0]
    assert "Micah Weekly Update" in spec.subject
    # Digest content: current score + weekly delta + index link.
    assert "War Index 2026" in spec.text
    assert "60.0" in spec.text
    assert "+5.0 this week" in spec.text
    assert "http://web.test/micah/war" in spec.text
    # Per-subscriber unsubscribe link in both bodies, placeholder fully swapped.
    unsub = f"http://web.test/unsubscribe?token={token_a}"
    assert unsub in spec.text and unsub in spec.html
    assert "__UNSUBSCRIBE_URL__" not in spec.text
    assert "__UNSUBSCRIBE_URL__" not in spec.html

    async with session_factory() as s:
        row = (
            await s.execute(
                select(CoreSubscriber).where(CoreSubscriber.email == "a@example.com")
            )
        ).scalar_one()
        assert row.last_sent_at is not None
        gone = (
            await s.execute(
                select(CoreSubscriber).where(CoreSubscriber.email == "gone@example.com")
            )
        ).scalar_one()
        assert gone.last_sent_at is None
        launches = (
            await s.execute(
                select(CoreSubscriber).where(
                    CoreSubscriber.email == "launches@example.com"
                )
            )
        ).scalar_one()
        assert launches.last_sent_at is None
