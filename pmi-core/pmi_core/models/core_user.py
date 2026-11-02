"""core_users — Google SSO accounts, one row per Google subject.

`google_sub` (Google's stable OIDC subject id) is the identity key. Email is
informational — Google lets it change under the same subject — so it is
indexed but NOT unique. Rows are created/refreshed by pmi-api's
``POST /auth/google`` on every sign-in: pmi-web completes the OAuth code
exchange with Google server-side and posts the verified profile here with the
service API key, so this table never sees an unverified claim.

No password / role columns on purpose: possession of a valid Google id_token
is the only auth, and every account today is a plain visitor account. The
session itself lives in a pmi-web-signed cookie, not in this table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from pmi_core.models.base import Base


class CoreUser(Base):
    __tablename__ = "core_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Google's `sub` claim: ≤255 chars per OIDC spec, in practice a ~21-digit
    # numeric string. Stable across email / profile changes.
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # RFC 5321 max address length; normalised to lowercase before insert.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256))
    picture_url: Mapped[str | None] = mapped_column(String(1024))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Stamped on every successful sign-in (the upsert's UPDATE path).
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_core_users__email", "email"),)
