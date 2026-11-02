"""pmi_api.entitlements — the single read point for limits (CORR-6.9 (1)).

Pure tests: no DB, no client fixture. The last test pins the architectural
invariant itself — no module outside entitlements.py/config.py may read a
quota or rate-limit knob directly — so a future route cannot quietly bypass
the read point that plans (CORR-8.8) will override.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import pmi_api
from pmi_api.config import api_settings
from pmi_api.entitlements import Limits, resolve_limits


def test_defaults_follow_settings(monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 7)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 12)
    monkeypatch.setattr(api_settings, "user_key_rpm", 90)
    monkeypatch.setattr(api_settings, "user_keys_max", 4)
    monkeypatch.setattr(api_settings, "reference_max_days", 180)
    limits = resolve_limits(None, None)
    assert limits == Limits(
        ask_anon_daily_quota=7,
        ask_user_daily_quota=12,
        rpm=90,
        keys_max=4,
        reference_max_days=180,
    )
    assert limits.anon_ask_allowed and limits.user_ask_metered and not limits.rpm_unlimited


def test_settings_are_read_at_call_time(monkeypatch):
    # Tests (and ops) flip api_settings at runtime; the read point must follow.
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 2)
    assert resolve_limits().ask_anon_daily_quota == 2
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 9)
    assert resolve_limits().ask_anon_daily_quota == 9


def test_key_rate_limit_overrides_env(monkeypatch):
    monkeypatch.setattr(api_settings, "user_key_rpm", 60)
    assert resolve_limits(key=SimpleNamespace(rate_limit_per_minute=5)).rpm == 5
    # A row without the attribute (or None) falls back to the env default.
    assert resolve_limits(key=SimpleNamespace()).rpm == 60
    assert resolve_limits(key=SimpleNamespace(rate_limit_per_minute=None)).rpm == 60
    assert resolve_limits(key=None).rpm == 60


def test_nonpositive_rpm_means_unlimited(monkeypatch):
    monkeypatch.setattr(api_settings, "user_key_rpm", 60)
    for raw in (0, -3):
        limits = resolve_limits(key=SimpleNamespace(rate_limit_per_minute=raw))
        assert limits.rpm == 0 and limits.rpm_unlimited
    monkeypatch.setattr(api_settings, "user_key_rpm", -1)
    assert resolve_limits().rpm_unlimited


def test_quota_semantics_match_the_settings_doc(monkeypatch):
    # anon 0 = sign-in wall; user <= 0 = unlimited — kept as raw ints so the
    # callers' existing `<= 0` checks and the /ask/questions payload are unchanged.
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 0)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", -1)
    limits = resolve_limits(SimpleNamespace(google_sub="x"))
    assert limits.ask_anon_daily_quota == 0 and not limits.anon_ask_allowed
    assert limits.ask_user_daily_quota == -1 and not limits.user_ask_metered


def test_identity_is_accepted_but_inert_until_plans_exist():
    anon = resolve_limits(None)
    assert resolve_limits("google-sub") == anon
    assert resolve_limits(SimpleNamespace(google_sub="s", email="e@x")) == anon


def test_limits_is_immutable():
    limits = resolve_limits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.rpm = 1  # type: ignore[misc]


_DIRECT_READ = re.compile(
    r"api_settings\.ask_(anon|user)_daily_quota"
    r"|\.rate_limit_per_minute"
    r"|USER_KEYS_MAX|USER_KEY_RPM|REFERENCE_MAX_DAYS"
)


def test_limits_have_one_read_point():
    """No pmi_api module except entitlements.py (and the settings that define
    the env defaults) may read a quota / rate-limit knob directly."""
    pkg = Path(pmi_api.__file__).resolve().parent
    offenders = sorted(
        str(p.relative_to(pkg))
        for p in pkg.rglob("*.py")
        if p.name not in {"entitlements.py", "config.py"}
        and _DIRECT_READ.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == []
