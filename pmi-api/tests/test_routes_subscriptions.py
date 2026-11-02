"""/subscriptions route tests — topics, idempotency, unsubscribe, reactivate."""

from __future__ import annotations

from pmi_core.models import CoreSubscriber
from sqlalchemy import select


async def test_subscribe_unsubscribe_resubscribe_flow(client, session_factory):
    # Subscribe — address is normalised (trim + lowercase); default topic.
    r = await client.post("/subscriptions", json={"email": "  Reader@Example.COM "})
    assert r.status_code == 200
    assert r.json()["data"] == {
        "email": "reader@example.com",
        "topic": "weekly-report",
        "status": "active",
    }

    async with session_factory() as s:
        row = (
            await s.execute(
                select(CoreSubscriber).where(CoreSubscriber.email == "reader@example.com")
            )
        ).scalar_one()
        token = row.unsubscribe_token
        assert row.status == "active"
        assert row.topic == "weekly-report"
        assert len(token) > 20

    # Second subscribe with the same (email, topic) is a no-op, not a dup row.
    r2 = await client.post("/subscriptions", json={"email": "reader@example.com"})
    assert r2.status_code == 200
    assert "already subscribed" in r2.json()["summary"]
    async with session_factory() as s:
        count = len(
            (
                await s.execute(
                    select(CoreSubscriber).where(
                        CoreSubscriber.email == "reader@example.com"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert count == 1

    # Unsubscribe via the token from the email footer link.
    r3 = await client.post("/subscriptions/unsubscribe", json={"token": token})
    assert r3.status_code == 200
    assert r3.json()["data"]["status"] == "unsubscribed"
    assert r3.json()["data"]["topic"] == "weekly-report"
    async with session_factory() as s:
        row = (
            await s.execute(
                select(CoreSubscriber).where(CoreSubscriber.email == "reader@example.com")
            )
        ).scalar_one()
        assert row.status == "unsubscribed"
        assert row.unsubscribed_at is not None

    # Re-subscribing reactivates the same row in place.
    r4 = await client.post("/subscriptions", json={"email": "reader@example.com"})
    assert r4.status_code == 200
    assert "re-subscribed" in r4.json()["summary"]
    async with session_factory() as s:
        row = (
            await s.execute(
                select(CoreSubscriber).where(CoreSubscriber.email == "reader@example.com")
            )
        ).scalar_one()
        assert row.status == "active"
        assert row.unsubscribed_at is None


async def test_one_email_can_hold_both_topics(client, session_factory):
    for topic in ["new-index", "weekly-report"]:
        r = await client.post(
            "/subscriptions", json={"email": "both@example.com", "topic": topic}
        )
        assert r.status_code == 200
        assert r.json()["data"]["topic"] == topic

    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(CoreSubscriber).where(
                        CoreSubscriber.email == "both@example.com"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(r.topic for r in rows) == ["new-index", "weekly-report"]
        # Unsubscribing one topic's token must not touch the other stream.
        new_index_token = next(r.unsubscribe_token for r in rows if r.topic == "new-index")

    r = await client.post("/subscriptions/unsubscribe", json={"token": new_index_token})
    assert r.status_code == 200
    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(CoreSubscriber).where(
                        CoreSubscriber.email == "both@example.com"
                    )
                )
            )
            .scalars()
            .all()
        )
        by_topic = {r.topic: r.status for r in rows}
        assert by_topic == {"new-index": "unsubscribed", "weekly-report": "active"}


async def test_subscribe_rejects_invalid_email(client):
    for bad in ["not-an-email", "a@b", "spaces in@addr.com"]:
        r = await client.post("/subscriptions", json={"email": bad})
        assert r.status_code == 422, bad


async def test_subscribe_rejects_unknown_topic(client):
    r = await client.post(
        "/subscriptions", json={"email": "ok@example.com", "topic": "daily-spam"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_TOPIC"


async def test_unsubscribe_unknown_token_404(client):
    r = await client.post("/subscriptions/unsubscribe", json={"token": "nope-" + "x" * 20})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "TOKEN_NOT_FOUND"


async def test_subscribe_profile_fields_persist_and_merge(client, session_factory):
    """The site-v2 modal sends an optional profile; an email-only re-subscribe
    must not wipe it, and a later modal submit overwrites what it sends."""
    r = await client.post(
        "/subscriptions",
        json={
            "email": "jane@example.com",
            "first_name": " Jane ",
            "last_name": "Doe",
            "institution": "",
            "interests": ["war", "maga", " btc "],
        },
    )
    assert r.status_code == 200
    async with session_factory() as s:
        row = (
            await s.execute(select(CoreSubscriber).where(CoreSubscriber.email == "jane@example.com"))
        ).scalar_one()
        assert row.first_name == "Jane"
        assert row.last_name == "Doe"
        assert row.institution is None  # blank → NULL, never ""
        assert row.interests == ["war", "maga", "btc"]

    # Email-only (footer form) resubmit: profile untouched.
    r2 = await client.post("/subscriptions", json={"email": "jane@example.com"})
    assert r2.status_code == 200
    async with session_factory() as s:
        row = (
            await s.execute(select(CoreSubscriber).where(CoreSubscriber.email == "jane@example.com"))
        ).scalar_one()
        assert row.first_name == "Jane"
        assert row.interests == ["war", "maga", "btc"]

    # Modal resubmit overwrites only the fields it sends.
    r3 = await client.post(
        "/subscriptions",
        json={"email": "jane@example.com", "institution": "Acme Fund", "interests": ["sp500"]},
    )
    assert r3.status_code == 200
    async with session_factory() as s:
        row = (
            await s.execute(select(CoreSubscriber).where(CoreSubscriber.email == "jane@example.com"))
        ).scalar_one()
        assert row.first_name == "Jane"
        assert row.institution == "Acme Fund"
        assert row.interests == ["sp500"]
