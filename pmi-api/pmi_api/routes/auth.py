"""/auth — Google SSO account upsert (server-to-server, called by pmi-web).

POST /auth/google  {google_sub, email, name?, picture_url?}
    → create-or-refresh the core_users row for that Google subject and stamp
      last_login_at. Returns the canonical row (id included) so the caller can
      embed it in its session cookie.

The browser never reaches this endpoint. pmi-web's /api/auth/google/callback
completes the OAuth code exchange with Google server-side (client secret and
id_token validation live there), then posts the *verified* profile here with
the service API key — same trust model as /subscriptions. This router only
records the account; the session itself is a pmi-web-signed cookie, so there
is no token issuance or lookup on this side.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pmi_core.models import CoreUser
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api.deps import get_session, require_api_key

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    dependencies=[Depends(require_api_key)],
)

# Same deliberately-loose shape as /subscriptions: Google already verified the
# address, this only keeps obvious garbage out of the table.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GoogleLoginRequest(BaseModel):
    google_sub: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=256)
    picture_url: str | None = Field(default=None, max_length=1024)


def _user_payload(row: CoreUser) -> dict:
    return {
        "id": row.id,
        "google_sub": row.google_sub,
        "email": row.email,
        "name": row.name,
        "picture_url": row.picture_url,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
    }


@router.post("/google")
async def google_login(
    body: GoogleLoginRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_EMAIL", "hint": "Provide a valid email address"},
        )
    google_sub = body.google_sub.strip()
    if not google_sub:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_SUB", "hint": "google_sub must be non-empty"},
        )

    now = datetime.now(UTC)
    row = (
        await session.execute(select(CoreUser).where(CoreUser.google_sub == google_sub))
    ).scalar_one_or_none()

    if row is None:
        row = CoreUser(
            google_sub=google_sub,
            email=email,
            name=body.name,
            picture_url=body.picture_url,
            last_login_at=now,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent first-sign-in race on the same subject: the other
            # request won unique(google_sub) — fall through to its row.
            await session.rollback()
            row = (
                await session.execute(
                    select(CoreUser).where(CoreUser.google_sub == google_sub)
                )
            ).scalar_one()
            row.last_login_at = now
            await session.commit()
        return {
            "summary": f"{email} signed in (new account)",
            "data": _user_payload(row),
        }

    # Refresh the profile Google reported this time — email/name/picture all
    # may drift under a stable subject.
    row.email = email
    row.name = body.name
    row.picture_url = body.picture_url
    row.last_login_at = now
    await session.commit()
    return {"summary": f"{email} signed in", "data": _user_payload(row)}
