"""Entitlements — the ONE place pmi-api reads a caller's limits (CORR-6.9 (1)).

Why a module for five numbers: every public capability has to be
paywall-ready from day one (CLAUDE.md §9 invariants, roadmap §2.2) — an
env-configured limit, server-side usage, and a single read point, so that when
plans arrive (CORR-8.8: ``core_plans`` + ``core_users.plan_id``) they override
here and nowhere else. Routes therefore never read
``api_settings.ask_*_daily_quota`` or ``CoreApiKey.rate_limit_per_minute``
directly; ``tests/test_entitlements.py`` pins that with a source scan.

Resolution today (no plans yet):

    env default (``ApiSettings``)  →  per-key override (``rate_limit_per_minute``)

``<= 0`` keeps the meaning the settings document for each knob (anon quota 0 =
sign-in wall, user quota <= 0 = unlimited, rpm <= 0 = unlimited). Pure
functions, no DB, no I/O — cheap enough to call once per request, and the
settings are read at call time so tests can monkeypatch ``api_settings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pmi_api.config import api_settings


@dataclass(frozen=True, slots=True)
class Limits:
    """A caller's effective limits. Immutable: resolve, don't mutate."""

    # /ask answered questions per day (routes/ask.py). anon: 0 = anonymous
    # asking off (sign-in wall); user: <= 0 = unlimited for signed-in accounts.
    ask_anon_daily_quota: int
    ask_user_daily_quota: int
    # Declared per-key requests per minute; 0 = unlimited. Declared only — the
    # meter that enforces it is CORR-6.9 (3).
    rpm: int
    # Self-service API keys one account may hold (SHIP-2.3a / CORR-6.11).
    keys_max: int
    # Deepest history (days) the reference layer serves per request (SHIP-5.1).
    reference_max_days: int

    @property
    def anon_ask_allowed(self) -> bool:
        return self.ask_anon_daily_quota > 0

    @property
    def user_ask_metered(self) -> bool:
        return self.ask_user_daily_quota > 0

    @property
    def rpm_unlimited(self) -> bool:
        return self.rpm <= 0


def resolve_limits(user: Any | None = None, key: Any | None = None) -> Limits:
    """The limits that apply to this caller.

    ``user`` is the caller's identity (a pmi-web ``AskUserRef``, a ``CoreUser``,
    a google_sub string …) and is accepted but unused today: it is the hook
    where a plan lookup (CORR-8.8) will override the env defaults. ``key`` is
    the ``CoreApiKey`` row that authenticated the request, when the route has
    one; its ``rate_limit_per_minute`` overrides the env rpm (``<= 0`` →
    unlimited). The pmi-web service key never changes /ask quotas — those are
    per identity, so /ask passes ``key=None``.
    """
    s = api_settings
    rpm = s.user_key_rpm
    key_rpm = getattr(key, "rate_limit_per_minute", None) if key is not None else None
    if key_rpm is not None:
        rpm = int(key_rpm)
    return Limits(
        ask_anon_daily_quota=s.ask_anon_daily_quota,
        ask_user_daily_quota=s.ask_user_daily_quota,
        rpm=max(0, rpm),
        keys_max=max(0, s.user_keys_max),
        reference_max_days=max(0, s.reference_max_days),
    )
