"""/ask — the AI-native "Ask Micah" surface (design: ASK VIEW).

v1 (2026-08-01) shipped preset questions only; v4 (2026-08-22) opens
free-form text. Both paths run ask_agent's fixed tool plans (presets by id,
free-form via the deterministic keyword router) — the model still never
chooses tools, so arbitrary text can steer WHICH index's data is shown but
never what gets executed. Free-form has a kill-switch
(PMI_API_ASK_FREEFORM_ENABLED=false → the original "not open yet" notice).

Daily quotas (2026-10-27, replacing the lifetime 20-per-account quota and the
PMI_WEB_LOGIN_DISABLED / PMI_API_ASK_REQUIRE_LOGIN toggles): anyone can ask.
An anonymous browser gets `ask_anon_daily_quota` answered questions per day
(default 5) and a signed-in account `ask_user_daily_quota` (default 10) in
total, counting what that browser already asked anonymously the same day. The
day is the calendar day in `ask_quota_timezone` (US Eastern). The pmi-web proxy
forwards the identities — `anon` from its signed `micah_anon` cookie, `user`
from the Google session — and this endpoint is service-key gated, so neither
can come from a browser directly. The quota numbers are read through
``pmi_api.entitlements.resolve_limits()`` — the single read point every limit
goes through (CORR-6.9) — never from ``api_settings`` here.

Server-side wall (2026-08-28 audit, finding #1): when auth is enabled
(`PMI_API_REQUIRE_AUTH=true`, the prod default) POST /ask refuses a caller with
no identity at all, and refuses anonymous callers when anonymous asking is off
(`ask_anon_daily_quota` <= 0), so a direct hit on pmi-api can never get a free,
unmetered answer. Dev (`require_auth=false`) is unaffected. Edge
defense-in-depth: the Caddyfile `api.*` vhost blocks /ask, /auth and
/subscriptions (the pmi-web proxy reaches pmi-api on the internal docker
hostname, never through that public name).

A slot is reserved in core_ask_log BEFORE the agent runs (advisory locks make
the count-then-insert race-safe) and refunded when no real answer comes back
(pipeline error, out-of-scope pointer). Cached answers still charge: the cache
is a server-side cost optimisation, not a different product event.

Answers cache in-process for `ask_cache_ttl_s` seconds — repeated asks
re-serve the same answer instead of re-running the model (spares the shared
GPU; a hosted PMI_API_ASK_MODEL bills per call). Preset entries are keyed by
id, free-form by a hash of the normalised text, capped at _CACHE_MAX rows.
Quota state is per-visitor and therefore attached per-response, never cached.

The browser never calls this directly — the pmi-web proxy (/api/ask)
re-issues requests with the service key, same pattern as /subscriptions.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pmi_core.models import CoreAskLog, CoreUser
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_api import ask_agent
from pmi_api.config import api_settings
from pmi_api.deps import get_session, require_api_key
from pmi_api.entitlements import resolve_limits

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"], dependencies=[Depends(require_api_key)])

DISABLED_MESSAGE = "目前尚未開啟此功能"

# Preset questions are the whole askable surface for now. What data each one
# needs (its pmi-mcp tool plan + unit-semantics note) lives with the agent —
# see ask_agent.PLANS, keyed by these ids.
PRESETS: list[dict] = [
    {
        "id": "war-now",
        "text": "What is the Global War Index right now, and what is driving it?",
    },
    {
        "id": "senate-2026",
        "text": "How many Senate seats are Republicans projected to win in 2026?",
    },
    {
        "id": "btc-stance",
        "text": "Is the market bullish or bearish on Bitcoin right now?",
    },
    {
        "id": "moved-week",
        "text": "What moved across Micah's indexes this week?",
    },
]

_PRESETS_BY_ID = {p["id"]: p for p in PRESETS}

# cache key -> (monotonic deadline, data payload). Presets key by id,
# free-form by text hash; bounded so arbitrary text can't grow it unbounded.
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 64


def _cache_put(key: str, deadline: float, data: dict) -> None:
    now = time.monotonic()
    for k in [k for k, (dl, _) in _cache.items() if dl <= now]:
        del _cache[k]
    while len(_cache) >= _CACHE_MAX:
        del _cache[next(iter(_cache))]  # oldest insertion first
    _cache[key] = (deadline, data)


class AskUserRef(BaseModel):
    """The signed-in account, forwarded by the trusted pmi-web proxy."""

    google_sub: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=256)


ANON_ID_PATTERN = r"^[a-f0-9]{32}$"


class AskAnonRef(BaseModel):
    """The anonymous browser, forwarded by the trusted pmi-web proxy from its
    signed micah_anon cookie (128 random bits, hex)."""

    id: str = Field(pattern=ANON_ID_PATTERN)


class AskRequest(BaseModel):
    question_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=2000)
    user: AskUserRef | None = None
    anon: AskAnonRef | None = None


def _is_out_of_scope(model: object, consulted: object) -> bool:
    """Free-form questions the router couldn't map run no LLM at all —
    ask_agent returns the fixed pointer with an empty model + lineage."""
    return not model and not consulted


async def _resolve_user(session: AsyncSession, ref: AskUserRef) -> CoreUser:
    """The account row for a session identity, creating it if missing.

    Normally /auth/google created it at sign-in; the create path covers a
    session cookie outliving a rebuilt core_users table (cookies are signed
    client-side), so the quota still binds to the stable Google subject.
    """
    row = (
        await session.execute(select(CoreUser).where(CoreUser.google_sub == ref.google_sub))
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = CoreUser(google_sub=ref.google_sub, email=ref.email.strip().lower(), name=ref.name)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent create on the same subject: the other request won.
        await session.rollback()
        row = (
            await session.execute(
                select(CoreUser).where(CoreUser.google_sub == ref.google_sub)
            )
        ).scalar_one()
    return row


def _quota_day(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start of today, next reset) for the quota day, both timezone-aware.

    The day is the calendar day in `ask_quota_timezone`. Adding a day to an
    aware local datetime is wall-clock arithmetic, so the reset stays at local
    midnight across DST changes.
    """
    tz = ZoneInfo(api_settings.ask_quota_timezone)
    local = (now or datetime.now(UTC)).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def _used_today(
    session: AsyncSession, user_id: int | None, anon_id: str | None, since: datetime
) -> int:
    """Answered questions since `since` by the account OR the browser.

    For an account this also counts the anonymous questions of every browser
    it has asked from since `since`, so the account's daily total is the same
    whichever browser it signs in on.
    """
    who = []
    if user_id is not None:
        who.append(CoreAskLog.user_id == user_id)
        who.append(
            CoreAskLog.anon_id.in_(
                select(CoreAskLog.anon_id).where(
                    CoreAskLog.user_id == user_id,
                    CoreAskLog.asked_at >= since,
                    CoreAskLog.anon_id.is_not(None),
                )
            )
        )
    if anon_id is not None:
        who.append(CoreAskLog.anon_id == anon_id)
    if not who:
        return 0
    return (
        await session.execute(
            select(func.count())
            .select_from(CoreAskLog)
            .where(CoreAskLog.asked_at >= since, or_(*who))
        )
    ).scalar_one()


def _quota_status(limit: int, used: int, scope: str, resets_at: datetime) -> dict:
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "scope": scope,
        "resets_at": resets_at.isoformat(),
    }


async def _charge_quota(
    session: AsyncSession,
    ref: AskUserRef | None,
    anon: AskAnonRef | None,
    *,
    kind: str,
    preset_id: str | None,
    question: str | None,
) -> tuple[dict | None, int | None]:
    """Reserve one of today's slots; returns (quota_status, reserved_row_id).

    A signed-in caller is charged against the user quota, counting the
    account's rows and the browser's anonymous rows from today; an anonymous
    caller against the anon quota. (None, None) when the caller has no
    identity (SSO-off dev) or its quota is disabled. Raises a coded 429 when
    today's questions are used up.
    """
    anon_id = anon.id if anon is not None else None
    if ref is None and anon_id is None:
        return None, None
    scope = "user" if ref is not None else "anon"
    limits = resolve_limits(ref)
    limit = limits.ask_user_daily_quota if ref is not None else limits.ask_anon_daily_quota
    if limit <= 0:
        return None, None
    user = await _resolve_user(session, ref) if ref is not None else None
    # Serialise per identity so parallel requests can't slip past the cap
    # between the count and the insert. Always account first, then browser, so
    # two requests can't wait on each other's locks. Transaction-scoped:
    # released at the commit/rollback just below, never held across the LLM.
    keys = ([f"user:{user.id}"] if user is not None else []) + (
        [f"anon:{anon_id}"] if anon_id is not None else []
    )
    for key in keys:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('core_ask_log:' || :k, 0))"),
            {"k": key},
        )
    since, resets_at = _quota_day()
    used = await _used_today(session, user.id if user is not None else None, anon_id, since)
    if used >= limit:
        await session.rollback()
        if scope == "anon":
            error = {
                "code": "ASK_TRIAL_EXHAUSTED",
                "hint": (
                    f"today's {limit} free questions are used; sign in with Google "
                    f"for {limits.ask_user_daily_quota} a day"
                ),
            }
        else:
            error = {
                "code": "ASK_QUOTA_EXCEEDED",
                "hint": f"today's {limit} questions are used; they reset at midnight",
            }
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": error,
                "quota": _quota_status(limit, used, scope, resets_at),
                "sign_in": scope == "anon",
            },
        )
    row = CoreAskLog(
        user_id=user.id if user is not None else None,
        anon_id=anon_id,
        kind=kind,
        preset_id=preset_id,
        question=(question or "")[:512] or None,
    )
    session.add(row)
    await session.commit()
    return _quota_status(limit, used + 1, scope, resets_at), row.id


async def _refund_quota(session: AsyncSession, reserved_id: int | None) -> None:
    """Best-effort: give the reserved slot back when no answer was produced."""
    if reserved_id is None:
        return
    try:
        await session.execute(delete(CoreAskLog).where(CoreAskLog.id == reserved_id))
        await session.commit()
    except Exception:
        log.warning("ask.quota_refund_failed", reserved_id=reserved_id, exc_info=True)


@router.get("/questions")
async def list_questions(
    google_sub: str | None = None,
    anon_id: str | None = Query(default=None, pattern=ANON_ID_PATTERN),
    session: AsyncSession = Depends(get_session),
) -> dict:
    freeform = api_settings.ask_freeform_enabled
    limits = resolve_limits(google_sub)
    anon_limit = limits.ask_anon_daily_quota
    user_limit = limits.ask_user_daily_quota
    since, resets_at = _quota_day()
    data: dict = {
        "questions": [{"id": p["id"], "text": p["text"]} for p in PRESETS],
        "freeform_enabled": freeform,
        "disabled_message": DISABLED_MESSAGE,
        # Whether anonymous visitors may ask at all; off means a sign-in wall.
        "trial": {
            "enabled": anon_limit > 0,
            "anon_daily_quota": anon_limit,
            "user_daily_quota": user_limit,
            "resets_at": resets_at.isoformat(),
        },
    }
    # Read-only quota status for the ask page's initial render. pmi-web passes
    # the session's google_sub and/or the browser's anon id server-side; a
    # first-time anonymous visitor (no cookie yet) gets the untouched anon
    # quota. Never creates the account row.
    if google_sub:
        if user_limit > 0:
            user = (
                await session.execute(select(CoreUser).where(CoreUser.google_sub == google_sub))
            ).scalar_one_or_none()
            used = await _used_today(
                session, user.id if user is not None else None, anon_id, since
            )
            data["quota"] = _quota_status(user_limit, used, "user", resets_at)
    elif anon_limit > 0:
        used = await _used_today(session, None, anon_id, since)
        data["quota"] = _quota_status(anon_limit, used, "anon", resets_at)
    return {
        "summary": (
            f"{len(PRESETS)} preset questions; free-form asking is "
            + ("open" if freeform else "not open yet")
        ),
        "data": data,
    }


async def _answer(label: str, runner) -> ask_agent.AskResult:
    """Run one agent call with the shared error contract (coded 503s)."""
    try:
        return await runner()
    except ask_agent.AskUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "ASK_UNAVAILABLE", "hint": str(exc)}},
        ) from exc
    except Exception as exc:
        # LLM or MCP outage: a coded 503 the UI can phrase, not a raw 500.
        log.error("ask.failed", question=label, error=str(exc), exc_info=True)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "ASK_FAILED",
                    "hint": "the answer pipeline errored; try again shortly",
                }
            },
        ) from exc


@router.post("")
async def ask(body: AskRequest, session: AsyncSession = Depends(get_session)) -> dict:
    # Server-side wall (defense in depth): when auth is enabled the only
    # legitimate caller is the trusted pmi-web proxy, which forwards the
    # signed-in account as `user` and/or the browser as `anon`. Refuse a caller
    # with no identity, and an anonymous one while anonymous asking is off, so a
    # direct call on pmi-api can never obtain a free, unmetered answer. Dev
    # (require_auth=false) is unaffected; the quotas govern identified callers.
    limits = resolve_limits(body.user)
    anon_allowed = body.anon is not None and limits.anon_ask_allowed
    if api_settings.require_auth and body.user is None and not anon_allowed:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "AUTH_REQUIRED",
                    "hint": "Sign in with Google to ask Micah",
                }
            },
        )
    preset = _PRESETS_BY_ID.get(body.question_id) if body.question_id else None
    now = time.monotonic()

    if preset is None:
        q = (body.text or "").strip()
        if not q or not api_settings.ask_freeform_enabled:
            # Empty text, unknown id, or the kill-switch: the fixed notice.
            # Nothing was answered, so nothing is charged.
            return {
                "summary": "free-form ask is not open",
                "data": {
                    "status": "disabled",
                    "message": DISABLED_MESSAGE,
                    "question": q or None,
                },
            }
        key = "text:" + hashlib.sha256(q.lower().encode()).hexdigest()[:24]
        hit = _cache.get(key)
        if hit and hit[0] > now:
            data = dict(hit[1])
            data["cached"] = True
            if not _is_out_of_scope(data.get("model"), data.get("consulted")):
                quota, _ = await _charge_quota(
                    session, body.user, body.anon, kind="freeform", preset_id=None, question=q
                )
                if quota:
                    data["quota"] = quota
            return {"summary": "answered free-form (cached)", "data": data}
        quota, reserved = await _charge_quota(
            session, body.user, body.anon, kind="freeform", preset_id=None, question=q
        )
        try:
            result = await _answer(q[:80], lambda: ask_agent.run_freeform(q))
        except HTTPException:
            await _refund_quota(session, reserved)
            raise
        data = {
            "status": "answered",
            "question": q,
            "answer": result.answer,
            "consulted": result.consulted,
            "model": result.model,
            "answered_at": datetime.now(UTC).isoformat(),
            "cached": False,
        }
        _cache_put(key, now + api_settings.ask_cache_ttl_s, data)
        data = dict(data)
        if _is_out_of_scope(result.model, result.consulted):
            # The router matched nothing → fixed pointer, no LLM ran: free.
            await _refund_quota(session, reserved)
        elif quota:
            data["quota"] = quota
        return {
            "summary": (
                f"answered free-form via {len(result.consulted)} MCP tool calls"
            ),
            "data": data,
        }

    hit = _cache.get(preset["id"])
    if hit and hit[0] > now:
        quota, _ = await _charge_quota(
            session, body.user, body.anon, kind="preset", preset_id=preset["id"], question=preset["text"]
        )
        data = dict(hit[1])
        data["cached"] = True
        if quota:
            data["quota"] = quota
        return {"summary": f"answered '{preset['id']}' (cached)", "data": data}

    quota, reserved = await _charge_quota(
        session, body.user, body.anon, kind="preset", preset_id=preset["id"], question=preset["text"]
    )
    try:
        result = await _answer(preset["id"], lambda: ask_agent.run_ask(preset))
    except HTTPException:
        await _refund_quota(session, reserved)
        raise
    data = {
        "status": "answered",
        "question_id": preset["id"],
        "question": preset["text"],
        "answer": result.answer,
        "consulted": result.consulted,
        "model": result.model,
        "answered_at": datetime.now(UTC).isoformat(),
        "cached": False,
    }
    _cache_put(preset["id"], now + api_settings.ask_cache_ttl_s, data)
    data = dict(data)
    if quota:
        data["quota"] = quota
    return {
        "summary": (
            f"answered '{preset['id']}' via {len(result.consulted)} MCP tool calls"
        ),
        "data": data,
    }
