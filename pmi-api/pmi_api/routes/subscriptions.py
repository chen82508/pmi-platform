"""/subscriptions — the landing page's two email lists, keyed by `topic`.

POST /subscriptions              {email, topic,   → subscribe (idempotent per
                                  first_name?, last_name?, institution?,
                                  interests?}     profile fields are optional
                                                   and overwrite when sent;
                                                   (email, topic); a previously
                                                   unsubscribed row reactivates
                                                   in place, preserving
                                                   created_at).
POST /subscriptions/unsubscribe  {token}         → status flip keyed by the
                                                   opaque per-row token that
                                                   stream's email footer link
                                                   carries (cancels ONE topic).

Topics: "new-index" (hero "Get our updates" form → email on every new index
launch, `notify-new-indexes` job) and "weekly-report" (the "Know when the
world reprices" card → Monday digest, `weekly-report` job).

The browser never calls these directly — the pmi-web proxy routes
(/api/subscribe, /unsubscribe) re-issue the request with the service API key
attached, so both endpoints sit behind the same `require_api_key` gate as the
rest of the API.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pmi_core.models import CoreSubscriber
from pmi_core.models.core_subscriber import (
    STATUS_ACTIVE,
    STATUS_UNSUBSCRIBED,
    TOPIC_WEEKLY_REPORT,
    TOPICS,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key

router = APIRouter(
    prefix="/subscriptions",
    tags=["subscriptions"],
    dependencies=[Depends(require_api_key)],
)

# Deliberately loose (real validation is the delivery attempt); it only
# rejects obvious garbage so the table stays clean. Full RFC 5322 parsing
# would need the email-validator dep for marginal benefit here.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SubscribeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    topic: str = Field(default=TOPIC_WEEKLY_REPORT, max_length=32)
    # Optional profile (site-v2 "Tune your reading" modal; alembic 0027).
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    institution: str | None = Field(default=None, max_length=200)
    interests: list[str] | None = Field(default=None, max_length=24)


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def _apply_profile(row: CoreSubscriber, body: SubscribeRequest) -> None:
    """Copy the optional profile fields onto the row — only the ones sent, so
    an email-only re-subscribe never wipes a profile filled in earlier."""
    if body.first_name is not None:
        row.first_name = _clean(body.first_name)
    if body.last_name is not None:
        row.last_name = _clean(body.last_name)
    if body.institution is not None:
        row.institution = _clean(body.institution)
    if body.interests is not None:
        row.interests = [i.strip()[:64] for i in body.interests if i and i.strip()][:24]


class UnsubscribeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)


def _normalise(email: str) -> str:
    return email.strip().lower()


@router.post("")
async def subscribe(
    body: SubscribeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    email = _normalise(body.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_EMAIL", "hint": "Provide a valid email address"},
        )
    topic = body.topic.strip().lower()
    if topic not in TOPICS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_TOPIC",
                "hint": f"topic must be one of {sorted(TOPICS)}",
            },
        )

    row = (
        await session.execute(
            select(CoreSubscriber).where(
                CoreSubscriber.email == email, CoreSubscriber.topic == topic
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = CoreSubscriber(
            email=email,
            topic=topic,
            status=STATUS_ACTIVE,
            unsubscribe_token=secrets.token_urlsafe(32),
            source="landing",
        )
        _apply_profile(row, body)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent double-submit of the same address: the other request
            # won the unique(email, topic) race — ours is the idempotent case.
            await session.rollback()
        return {
            "summary": f"{email} subscribed to {topic}",
            "data": {"email": email, "topic": topic, "status": STATUS_ACTIVE},
        }

    if row.status != STATUS_ACTIVE:
        row.status = STATUS_ACTIVE
        row.unsubscribed_at = None
        _apply_profile(row, body)
        await session.commit()
        return {
            "summary": f"{email} re-subscribed to {topic}",
            "data": {"email": email, "topic": topic, "status": STATUS_ACTIVE},
        }

    # Already active: the modal may still be adding a profile to an address
    # that first came in through the email-only footer form.
    _apply_profile(row, body)
    await session.commit()
    return {
        "summary": f"{email} is already subscribed to {topic}",
        "data": {"email": email, "topic": topic, "status": STATUS_ACTIVE},
    }


@router.post("/unsubscribe")
async def unsubscribe(
    body: UnsubscribeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            select(CoreSubscriber).where(CoreSubscriber.unsubscribe_token == body.token)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "TOKEN_NOT_FOUND", "hint": "The unsubscribe link is invalid"},
        )

    if row.status != STATUS_UNSUBSCRIBED:
        row.status = STATUS_UNSUBSCRIBED
        row.unsubscribed_at = datetime.now(UTC)
        await session.commit()

    return {
        "summary": f"{row.email} unsubscribed from {row.topic}",
        "data": {"email": row.email, "topic": row.topic, "status": STATUS_UNSUBSCRIBED},
    }
