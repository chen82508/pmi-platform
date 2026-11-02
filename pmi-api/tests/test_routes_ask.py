"""/ask route tests — presets + free-form answered via the (mocked) agent."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pmi_core.models import CoreAskLog

from pmi_api import ask_agent
from pmi_api.config import api_settings
from pmi_api.routes import ask as ask_route


async def test_questions_lists_presets(client):
    r = await client.get("/ask/questions")
    assert r.status_code == 200
    body = r.json()
    qs = body["data"]["questions"]
    assert len(qs) >= 3
    assert {"id", "text"} <= set(qs[0])
    # v4 default: free-form is open (pmi-web's sign-in gate is the access
    # control); the flag mirrors the kill-switch setting.
    assert body["data"]["freeform_enabled"] is True
    assert body["data"]["disabled_message"] == "目前尚未開啟此功能"


async def test_freeform_disabled_by_kill_switch(client, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("agent must not run when the kill-switch is off")

    monkeypatch.setattr(api_settings, "ask_freeform_enabled", False)
    monkeypatch.setattr(ask_agent, "run_freeform", _boom)
    r = await client.post("/ask", json={"text": "will it rain tomorrow?"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "disabled"
    assert data["message"] == "目前尚未開啟此功能"
    assert data["question"] == "will it rain tomorrow?"
    q = await client.get("/ask/questions")
    assert q.json()["data"]["freeform_enabled"] is False


async def test_freeform_answers_and_caches(client, monkeypatch):
    calls: list[str] = []

    async def _fake(text: str):
        calls.append(text)
        return ask_agent.AskResult(
            answer="stub free-form answer",
            consulted=[{"tool": "get_index_score", "arguments": {"index_id": "x"}}],
            model="stub/model",
        )

    monkeypatch.setattr(ask_agent, "run_freeform", _fake)
    ask_route._cache.clear()
    r = await client.post("/ask", json={"text": "Is bitcoin looking bullish?"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "answered"
    assert data["question"] == "Is bitcoin looking bullish?"
    assert data["answer"] == "stub free-form answer"
    assert data["cached"] is False
    assert "question_id" not in data

    r2 = await client.post("/ask", json={"text": "Is bitcoin looking bullish?"})
    assert r2.json()["data"]["cached"] is True
    assert calls == ["Is bitcoin looking bullish?"]  # second hit served from cache


async def test_freeform_empty_text_is_still_the_notice(client, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("agent must not run for empty text")

    monkeypatch.setattr(ask_agent, "run_freeform", _boom)
    r = await client.post("/ask", json={"text": "   "})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "disabled"


async def test_unknown_question_id_is_disabled_too(client, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("agent must not run for unknown ids")

    monkeypatch.setattr(ask_agent, "run_ask", _boom)
    r = await client.post("/ask", json={"question_id": "nope"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "disabled"


def test_freeform_router_picks_indexes():
    assert ask_agent.route_freeform("Is bitcoin going to rip?") == [
        "btc-bull-bear-index"
    ]
    assert ask_agent.route_freeform("How do the midterms look for Republicans?") == [
        "us-senate-2026-republican-seats",
        "us-house-2026-republican-seats",
    ]
    # Specific chamber routes outrank the generic election route; cap at two.
    assert ask_agent.route_freeform(
        "senate, house and governor races plus the election"
    ) == [
        "us-senate-2026-republican-seats",
        "us-house-2026-republican-seats",
    ]
    # "White House" must not be read as the House-seats index.
    assert ask_agent.route_freeform("Who takes the White House?") == []
    assert ask_agent.route_freeform("what should I eat tonight") == []
    assert ask_agent.route_freeform("Any escalation risk around Taiwan?") == [
        "war-index-2026"
    ]


async def test_freeform_out_of_scope_answers_without_llm():
    # No route match → the fixed pointer, straight from Python: no MCP
    # session, no completion (nothing here is mocked — it must not need it).
    res = await ask_agent.run_freeform("what should I have for dinner tonight?")
    assert res.answer == ask_agent.OUT_OF_SCOPE_ANSWER
    assert res.consulted == []
    assert res.model == ""


def test_ask_model_env_switch(monkeypatch):
    """One env (PMI_API_ASK_MODEL) flips local ↔ chatgpt; full ids pass through."""
    from pmi_api.config import api_settings

    monkeypatch.setattr(api_settings, "ask_model", "local")
    assert api_settings.ask_model_resolved == api_settings.ask_local_model
    monkeypatch.setattr(api_settings, "ask_model", "ChatGPT")
    assert api_settings.ask_model_resolved == api_settings.ask_openai_model
    monkeypatch.setattr(api_settings, "ask_model", "ollama/qwen3:8b")
    assert api_settings.ask_model_resolved == "ollama/qwen3:8b"
    monkeypatch.setattr(api_settings, "ask_model", "gpt-4o-mini-2024-07-18")
    assert api_settings.ask_model_resolved == "gpt-4o-mini-2024-07-18"


async def test_every_preset_has_a_gather_plan():
    # The route owns the public preset list; the agent owns the tool plans.
    # A preset without a plan would 503 at runtime — catch it here instead.
    assert {p["id"] for p in ask_route.PRESETS} <= set(ask_agent.PLANS)


def test_single_index_plans_gather_level_drivers_and_trend():
    # Thoughtful answers need all three ingredients; a plan that drops one
    # silently degrades the answer back to a bare number restatement.
    for pid in ("war-now", "senate-2026", "btc-stance"):
        tools = {name for name, _ in ask_agent.PLANS[pid]["calls"]}
        assert {"get_index_score", "explain_index", "get_index_history"} <= tools, pid


def _history_block(index_id: str, first: float, last: float, lo: float, hi: float):
    return {
        "tool": "get_index_history",
        "args": {"index_id": index_id, "days": 7},
        "result": {
            "data": {
                "index_id": index_id,
                "days": 7,
                "first": {"score": first},
                "last": {"score": last},
                "min": lo,
                "max": hi,
                "delta": round(last - first, 2),
            }
        },
    }


def _score_block(index_id: str, score: float):
    return {
        "tool": "get_index_score",
        "args": {"index_id": index_id},
        "result": {"data": {"index_id": index_id, "score": score}},
    }


def _explain_block(index_id: str):
    return {
        "tool": "explain_index",
        "args": {"index_id": index_id, "top": 8},
        "result": {
            "data": {
                "index_id": index_id,
                "n_components": 42,
                "top_components": [
                    {
                        "title": "Russia x Ukraine ceasefire in 2026?",
                        "venue": "polymarket",
                        "last_price": 0.31,
                    },
                    {"title": "US x Iran conflict?", "venue": "kalshi", "last_price": 0.07},
                ],
            }
        },
    }


def test_derived_facts_war_cover_drivers_and_trend():
    blocks = [
        _score_block("war-index-2026", 15.1),
        _explain_block("war-index-2026"),
        _history_block("war-index-2026", 13.2, 15.1, 12.9, 15.4),
    ]
    facts = ask_agent._derived_facts("war-now", blocks)
    joined = " | ".join(facts)
    # The side is stated outright — qwen3 fused "(midpoint 50)" with "near
    # the top of its range" into "above the midpoint" twice in a row.
    assert "LOW level of priced conflict risk, below the 50 midpoint" in joined
    assert "aggregates 42 markets" in joined
    assert "Russia x Ukraine ceasefire in 2026?" in joined and "31%" in joined
    assert "rose +1.90" in joined
    assert "near the top of" in joined  # 15.1 in [12.9, 15.4]


def test_derived_facts_btc_include_stance_strength():
    blocks = [
        _score_block("btc-bull-bear-index", 32.2),
        _history_block("btc-bull-bear-index", 32.0, 32.2, 31.8, 32.5),
    ]
    facts = ask_agent._derived_facts("btc-stance", blocks)
    joined = " | ".join(facts)
    assert "BEARISH" in joined and "strong stance" in joined
    assert "17.8 points from the neutral line" in joined
    assert "held roughly flat" in joined


def test_derived_facts_senate_state_margin_to_majority():
    facts = ask_agent._derived_facts(
        "senate-2026", [_score_block("us-senate-2026-republican-seats", 51.53)]
    )
    assert any("outright Republican majority" in f for f in facts)
    assert any("+1.53 seats relative to the 50-seat control line" in f for f in facts)
    # 50.4 is Republican CONTROL (the VP breaks ties), not "short of 51".
    facts = ask_agent._derived_facts(
        "senate-2026", [_score_block("us-senate-2026-republican-seats", 50.4)]
    )
    assert any("enough for Republican control" in f for f in facts)
    assert not any("short of" in f and "control" not in f for f in facts)
    facts = ask_agent._derived_facts(
        "senate-2026", [_score_block("us-senate-2026-republican-seats", 49.2)]
    )
    assert any("short of the 50 seats Republicans need for control" in f for f in facts)
    assert any("-0.80 seats" in f for f in facts)


def test_derived_facts_moved_week_rank_and_market_movers():
    blocks = [
        _history_block("war-index-2026", 13.0, 15.0, 12.9, 15.2),
        _history_block("btc-bull-bear-index", 32.4, 32.2, 31.9, 32.6),
        {
            "tool": "get_top_movers",
            "args": {},
            "result": {
                "data": {
                    "movers": [
                        {"title": "Ceasefire by Sept 30?", "index": "War", "delta_pts": -8.0},
                        {"title": "BTC above 130k?", "index": "BTC", "delta_pts": 4.5},
                    ]
                }
            },
        },
    ]
    facts = ask_agent._derived_facts("moved-week", blocks)
    ranked = next(f for f in facts if f.startswith("7-day moves ranked"))
    # Biggest absolute move listed first.
    assert ranked.index("war-index-2026") < ranked.index("btc-bull-bear-index")
    movers = next(f for f in facts if "market-level colour" in f)
    assert '"Ceasefire by Sept 30?" (a War component) moved -8.0 pts in 24h' in movers


async def test_preset_flow_and_cache(client, monkeypatch):
    calls: list[str] = []

    async def _fake(preset):
        calls.append(preset["text"])
        return ask_agent.AskResult(
            answer="The War Index sits at 62 as of now.",
            consulted=[
                {"tool": "get_index_score", "arguments": {"index_id": "war-index-2026"}}
            ],
            model="test-model",
        )

    monkeypatch.setattr(ask_agent, "run_ask", _fake)
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]

    r = await client.post("/ask", json={"question_id": qid})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "answered"
    assert data["answer"].startswith("The War Index")
    assert data["consulted"][0]["tool"] == "get_index_score"
    assert data["model"] == "test-model"
    assert data["cached"] is False

    # Second hit within the TTL serves the cache — the agent runs once.
    r2 = await client.post("/ask", json={"question_id": qid})
    assert r2.status_code == 200
    assert r2.json()["data"]["cached"] is True
    assert len(calls) == 1
    ask_route._cache.clear()


async def test_answer_requires_identity_when_auth_enabled(client, monkeypatch):
    # Finding #1: pmi-api enforces the wall itself so a direct hit (e.g. the
    # api.* vhost) can't get a free, unmetered answer. With auth on, a POST with
    # no identity at all is refused BEFORE any agent/router runs — presets and
    # free-form alike. An anonymous browser id (the proxy's micah_anon cookie)
    # or a signed-in user is enough to be served.
    monkeypatch.setattr(api_settings, "require_auth", True)

    async def _boom(*a, **k):
        raise AssertionError("agent must not run for an unidentified caller")

    monkeypatch.setattr(ask_agent, "run_ask", _boom)
    monkeypatch.setattr(ask_agent, "run_freeform", _boom)
    ask_route._cache.clear()

    r = await client.post("/ask", json={"question_id": ask_route.PRESETS[0]["id"]})
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "AUTH_REQUIRED"

    r2 = await client.post("/ask", json={"text": "is bitcoin bullish?"})
    assert r2.status_code == 401
    assert r2.json()["detail"]["error"]["code"] == "AUTH_REQUIRED"

    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    qid = ask_route.PRESETS[0]["id"]
    r3 = await client.post("/ask", json={"question_id": qid, "user": _user("wall-ok")})
    assert r3.status_code == 200
    assert r3.json()["data"]["status"] == "answered"

    r4 = await client.post("/ask", json={"question_id": qid, "anon": _anon("wall-anon-ok")})
    assert r4.status_code == 200
    assert r4.json()["data"]["quota"]["scope"] == "anon"
    ask_route._cache.clear()


async def test_anonymous_asking_off_is_a_sign_in_wall(client, monkeypatch):
    # PMI_API_ASK_ANON_DAILY_QUOTA=0 turns anonymous asking off: an anon-only
    # caller gets the sign-in 401, a signed-in one is still served.
    monkeypatch.setattr(api_settings, "require_auth", True)
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 0)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]

    r = await client.post("/ask", json={"question_id": qid, "anon": _anon("wall-off")})
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "AUTH_REQUIRED"

    r2 = await client.post(
        "/ask", json={"question_id": qid, "user": _user("wall-off-user"), "anon": _anon("wall-off")}
    )
    assert r2.status_code == 200
    ask_route._cache.clear()


async def test_rejects_malformed_anon_id(client):
    r = await client.post(
        "/ask", json={"question_id": ask_route.PRESETS[0]["id"], "anon": {"id": "not-hex"}}
    )
    assert r.status_code == 422


async def test_agent_unavailable_maps_to_503(client, monkeypatch):
    async def _unavail(*a, **k):
        raise ask_agent.AskUnavailable("no LLM API key configured")

    monkeypatch.setattr(ask_agent, "run_ask", _unavail)
    ask_route._cache.clear()
    r = await client.post("/ask", json={"question_id": ask_route.PRESETS[0]["id"]})
    assert r.status_code == 503
    assert r.json()["detail"]["error"]["code"] == "ASK_UNAVAILABLE"


# ──────────────────────────────────────────────────────────────────────────
# Daily quotas (2026-10-27): anonymous browsers get PMI_API_ASK_ANON_DAILY_QUOTA
# a day, signed-in accounts PMI_API_ASK_USER_DAILY_QUOTA in total with the same
# browser's anonymous use. Ask-log rows persist for the whole test session, so
# every test uses its own google_sub / anon id.
# ──────────────────────────────────────────────────────────────────────────


def _user(sub: str) -> dict:
    return {"google_sub": sub, "email": f"{sub}@example.com", "name": "Quota Tester"}


def _anon(tag: str) -> dict:
    return {"id": hashlib.md5(tag.encode()).hexdigest()}


def _q(quota: dict) -> tuple:
    """The counting part of a quota block (resets_at is checked separately)."""
    return quota["limit"], quota["used"], quota["remaining"], quota["scope"]


def _fake_preset_answer():
    async def _fake(preset):
        return ask_agent.AskResult(
            answer="stub answer",
            consulted=[{"tool": "get_index_score", "arguments": {"index_id": "x"}}],
            model="stub/model",
        )

    return _fake


async def test_anon_daily_quota_charges_blocks_and_counts_cache_hits(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 2)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 5)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]
    anon = _anon("anon-basic")

    r1 = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r1.status_code == 200
    assert _q(r1.json()["data"]["quota"]) == (2, 1, 1, "anon")

    # Second ask serves the cache but still charges — the visitor asked again.
    r2 = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r2.json()["data"]["cached"] is True
    assert _q(r2.json()["data"]["quota"]) == (2, 2, 0, "anon")

    r3 = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r3.status_code == 429
    detail = r3.json()["detail"]
    assert detail["error"]["code"] == "ASK_TRIAL_EXHAUSTED"
    assert detail["sign_in"] is True
    assert _q(detail["quota"]) == (2, 2, 0, "anon")

    # Callers without any identity (SSO-off dev, require_auth=false) stay ungated.
    r4 = await client.post("/ask", json={"question_id": qid})
    assert r4.status_code == 200
    assert "quota" not in r4.json()["data"]
    ask_route._cache.clear()


async def test_sign_in_total_includes_todays_anonymous_use(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 2)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 3)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]
    anon, user = _anon("browser-then-sign-in"), _user("browser-then-sign-in")

    for _ in range(2):
        assert (await client.post("/ask", json={"question_id": qid, "anon": anon})).status_code == 200
    assert (await client.post("/ask", json={"question_id": qid, "anon": anon})).status_code == 429

    # Same browser signs in: the 2 anonymous questions count toward the 3.
    r = await client.post("/ask", json={"question_id": qid, "user": user, "anon": anon})
    assert r.status_code == 200
    assert _q(r.json()["data"]["quota"]) == (3, 3, 0, "user")

    r2 = await client.post("/ask", json={"question_id": qid, "user": user, "anon": anon})
    assert r2.status_code == 429
    assert r2.json()["detail"]["error"]["code"] == "ASK_QUOTA_EXCEEDED"
    assert r2.json()["detail"]["sign_in"] is False

    # The same account on another browser is still capped by the account total.
    r3 = await client.post(
        "/ask", json={"question_id": qid, "user": user, "anon": _anon("second-browser")}
    )
    assert r3.status_code == 429
    ask_route._cache.clear()


async def test_signing_out_does_not_hand_out_fresh_anonymous_questions(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 2)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 10)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]
    anon, user = _anon("sign-out"), _user("sign-out")

    for _ in range(2):
        r = await client.post("/ask", json={"question_id": qid, "user": user, "anon": anon})
        assert r.status_code == 200

    # Signed-in asks recorded the browser too, so after signing out it has no
    # anonymous questions left today.
    r2 = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r2.status_code == 429
    assert r2.json()["detail"]["error"]["code"] == "ASK_TRIAL_EXHAUSTED"
    ask_route._cache.clear()


async def test_yesterdays_questions_do_not_count(client, monkeypatch, session_factory):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 1)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    anon = _anon("yesterday")
    start, _ = ask_route._quota_day()
    async with session_factory() as session:
        session.add(
            CoreAskLog(
                anon_id=anon["id"],
                kind="preset",
                preset_id="x",
                asked_at=start - timedelta(minutes=1),
            )
        )
        await session.commit()

    r = await client.post("/ask", json={"question_id": ask_route.PRESETS[0]["id"], "anon": anon})
    assert r.status_code == 200
    assert _q(r.json()["data"]["quota"]) == (1, 1, 0, "anon")
    ask_route._cache.clear()


def test_quota_day_is_the_us_eastern_calendar_day(monkeypatch):
    monkeypatch.setattr(api_settings, "ask_quota_timezone", "America/New_York")
    # 03:30 UTC on 10-27 is still 10-26 in New York (EDT, UTC-4).
    start, reset = ask_route._quota_day(datetime(2026, 10, 27, 3, 30, tzinfo=UTC))
    assert start.astimezone(UTC) == datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
    assert reset.astimezone(UTC) == datetime(2026, 10, 27, 4, 0, tzinfo=UTC)
    # DST ends on 2026-11-01: that day is 25 hours and resets at local midnight.
    start, reset = ask_route._quota_day(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    assert start.astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert reset.astimezone(UTC) == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


async def test_user_quota_disabled_by_env(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 0)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    r = await client.post(
        "/ask",
        json={"question_id": ask_route.PRESETS[0]["id"], "user": _user("quota-off")},
    )
    assert r.status_code == 200
    assert "quota" not in r.json()["data"]
    ask_route._cache.clear()


async def test_quota_out_of_scope_freeform_is_free(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 1)
    ask_route._cache.clear()
    user = _user("quota-oos")

    # No route match → the fixed pointer straight from Python (no mock needed,
    # same as test_freeform_out_of_scope_answers_without_llm). Must not charge.
    r = await client.post(
        "/ask", json={"text": "what should I eat tonight?", "user": user}
    )
    assert r.status_code == 200
    assert r.json()["data"]["answer"] == ask_agent.OUT_OF_SCOPE_ANSWER
    assert "quota" not in r.json()["data"]

    # A cached replay of the out-of-scope answer is free too.
    r2 = await client.post(
        "/ask", json={"text": "what should I eat tonight?", "user": user}
    )
    assert r2.json()["data"]["cached"] is True
    assert "quota" not in r2.json()["data"]

    # The single slot is still available for a real (mocked) answer.
    async def _fake(text: str):
        return ask_agent.AskResult(
            answer="real answer", consulted=[{"tool": "get_index_score"}], model="m"
        )

    monkeypatch.setattr(ask_agent, "run_freeform", _fake)
    r3 = await client.post(
        "/ask", json={"text": "Is bitcoin bullish right now?", "user": user}
    )
    assert r3.status_code == 200
    assert _q(r3.json()["data"]["quota"]) == (1, 1, 0, "user")
    ask_route._cache.clear()


async def test_quota_refunds_when_the_agent_fails(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 1)
    ask_route._cache.clear()
    qid = ask_route.PRESETS[0]["id"]
    anon = _anon("quota-refund")

    async def _boom(preset):
        raise RuntimeError("llm down")

    monkeypatch.setattr(ask_agent, "run_ask", _boom)
    r = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r.status_code == 503

    # The failed ask was refunded, so the only slot is still usable.
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    r2 = await client.post("/ask", json={"question_id": qid, "anon": anon})
    assert r2.status_code == 200
    assert _q(r2.json()["data"]["quota"]) == (1, 1, 0, "anon")
    ask_route._cache.clear()


async def test_questions_reports_quota_status(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 5)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 10)
    # A first-time anonymous visitor (no cookie yet): the untouched anon quota.
    r = await client.get("/ask/questions")
    data = r.json()["data"]
    assert _q(data["quota"]) == (5, 0, 5, "anon")
    assert data["trial"]["enabled"] is True
    assert (data["trial"]["anon_daily_quota"], data["trial"]["user_daily_quota"]) == (5, 10)
    assert data["quota"]["resets_at"] == data["trial"]["resets_at"]

    # Unknown subject → the full user quota (the account row is NOT created).
    r2 = await client.get("/ask/questions", params={"google_sub": "quota-never-asked"})
    assert _q(r2.json()["data"]["quota"]) == (10, 0, 10, "user")

    # A malformed anon id is rejected rather than silently ignored.
    r3 = await client.get("/ask/questions", params={"anon_id": "nope"})
    assert r3.status_code == 422

    # Anonymous asking off: no anon quota block, and the trial says so.
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 0)
    r4 = await client.get("/ask/questions")
    assert "quota" not in r4.json()["data"]
    assert r4.json()["data"]["trial"]["enabled"] is False


async def test_questions_counts_the_browsers_use_for_a_signed_in_visitor(client, monkeypatch):
    monkeypatch.setattr(api_settings, "ask_anon_daily_quota", 5)
    monkeypatch.setattr(api_settings, "ask_user_daily_quota", 10)
    monkeypatch.setattr(ask_agent, "run_ask", _fake_preset_answer())
    ask_route._cache.clear()
    anon = _anon("questions-browser")
    await client.post("/ask", json={"question_id": ask_route.PRESETS[0]["id"], "anon": anon})

    r = await client.get("/ask/questions", params={"anon_id": anon["id"]})
    assert _q(r.json()["data"]["quota"]) == (5, 1, 4, "anon")
    r2 = await client.get(
        "/ask/questions", params={"google_sub": "questions-browser", "anon_id": anon["id"]}
    )
    assert _q(r2.json()["data"]["quota"]) == (10, 1, 9, "user")
    ask_route._cache.clear()


# ──────────────────────────────────────────────────────────────────────────
# v5 (2026-08-29): dynamic moved-week plan, native-ollama wording path, and
# the unrouted-question demand log.
# ──────────────────────────────────────────────────────────────────────────


def _catalog_block(ids: list[str]) -> dict:
    return {
        "tool": "list_indexes",
        "args": {},
        "result": {
            "summary": f"{len(ids)} live indexes",
            "data": [{"id": i, "version": 1, "title": i.title()} for i in ids],
        },
    }


async def test_moved_week_plan_follows_the_live_catalog(monkeypatch):
    gathers: list[list] = []

    async def _fake_gather(calls):
        gathers.append(list(calls))
        if calls == [("list_indexes", {})]:
            return (
                [{"tool": "list_indexes", "arguments": {}}],
                [
                    _catalog_block(
                        [
                            "war-index-2026",
                            "us-house-2026-republican-seats",
                            "btc-bull-bear-index",
                        ]
                    )
                ],
            )
        consulted = [{"tool": n, "arguments": a} for n, a in calls]
        blocks = [
            _history_block(a["index_id"], 10.0, 12.0, 9.0, 12.5)
            if n == "get_index_history"
            else {"tool": n, "args": a, "result": {"data": {"movers": []}}}
            for n, a in calls
        ]
        return consulted, blocks

    captured: dict = {}

    async def _fake_complete(question, note, facts, blocks, consulted):
        captured.update(facts=facts, blocks=blocks, consulted=consulted)
        return ask_agent.AskResult(answer="ok", consulted=consulted, model="m")

    monkeypatch.setattr(ask_agent, "_gather", _fake_gather)
    monkeypatch.setattr(ask_agent, "_complete", _fake_complete)
    await ask_agent.run_ask({"id": "moved-week", "text": "What moved this week?"})

    # Every catalog index gets a history pull — the hardcoded four missed
    # house-seats being the week's actual biggest mover (2026-08-29).
    hist = [a["index_id"] for n, a in gathers[1] if n == "get_index_history"]
    assert hist == [
        "war-index-2026",
        "us-house-2026-republican-seats",
        "btc-bull-bear-index",
    ]
    assert ("get_top_movers", {}) in gathers[1]
    # The catalog call is part of the answer's visible lineage.
    assert captured["consulted"][0]["tool"] == "list_indexes"
    # The ranked fact names indexes by their catalog title, not the raw id.
    ranked = next(f for f in captured["facts"] if f.startswith("7-day moves"))
    assert "War-Index-2026 moved +2.00" in ranked
    assert "war-index-2026 moved" not in ranked


async def test_moved_week_falls_back_when_catalog_errors(monkeypatch):
    gathers: list[list] = []

    async def _fake_gather(calls):
        gathers.append(list(calls))
        if calls == [("list_indexes", {})]:
            return (
                [{"tool": "list_indexes", "arguments": {}}],
                [
                    {
                        "tool": "list_indexes",
                        "args": {},
                        "result": {
                            "error": {"code": "PMI_API_UNREACHABLE", "hint": ""}
                        },
                    }
                ],
            )
        return ([], [])

    async def _fake_complete(question, note, facts, blocks, consulted):
        return ask_agent.AskResult(answer="ok", consulted=consulted, model="m")

    monkeypatch.setattr(ask_agent, "_gather", _fake_gather)
    monkeypatch.setattr(ask_agent, "_complete", _fake_complete)
    await ask_agent.run_ask({"id": "moved-week", "text": "What moved this week?"})
    hist = tuple(a["index_id"] for n, a in gathers[1] if n == "get_index_history")
    assert hist == ask_agent._MOVED_WEEK_FALLBACK_IDS


def test_moved_week_ranked_fact_caps_the_list():
    # deltas +1.00 … +7.00 — top five quoted, the tail folded into one line.
    blocks = [
        _history_block(f"idx-{k}", 10.0, 10.0 + k, 9.0, 20.0) for k in range(1, 8)
    ]
    facts = ask_agent._derived_facts("moved-week", blocks)
    ranked = next(f for f in facts if f.startswith("7-day moves ranked"))
    quoted = ranked.split("; the remaining")[0]
    assert "idx-7 moved +7.00" in quoted and "idx-3 moved +3.00" in quoted
    assert "idx-2" not in quoted
    assert "the remaining 2 tracked indexes each moved 2.00 or less" in ranked


def test_chat_request_uses_native_ollama_api(monkeypatch):
    # ollama/*: native /api/chat with thinking off — the /v1 endpoint cannot
    # disable qwen3-class thinking, which is pure latency for wording.
    monkeypatch.setattr(api_settings, "ask_max_tokens", 700)
    msgs = [{"role": "user", "content": "q"}]
    url, headers, body = ask_agent._chat_request("ollama/qwen3:8b", msgs)
    assert url.endswith("/api/chat") and "/v1/" not in url
    assert headers == {}
    assert body["model"] == "qwen3:8b"
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {
        "temperature": 0.1,
        "num_predict": 700,
        "num_ctx": ask_agent._OLLAMA_NUM_CTX,
    }

    # Non-ollama ids keep the OpenAI-compatible shape.
    from pmi_core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "llm_api_key", "test-key")
    url2, headers2, body2 = ask_agent._chat_request("gpt-4o-mini-2024-07-18", msgs)
    assert url2.endswith("/chat/completions")
    assert headers2 == {"Authorization": "Bearer test-key"}
    assert body2["max_tokens"] == 700 and body2["temperature"] == 0.1
    assert "think" not in body2


def test_answer_text_parses_both_endpoint_shapes():
    assert ask_agent._answer_text({"message": {"content": " hi "}}) == "hi"
    assert ask_agent._answer_text({"choices": [{"message": {"content": "yo"}}]}) == "yo"
    # Markdown bold slips through small models despite the style rule; the
    # ask page renders raw text, so the markers are stripped server-side.
    assert ask_agent._answer_text({"message": {"content": "**bold** kept"}}) == "bold kept"


async def test_unrouted_freeform_logs_the_question(monkeypatch):
    # The refund path deletes the ledger row, so this log line is the only
    # measurable demand signal for unrouted questions.
    events: list[dict] = []
    monkeypatch.setattr(
        ask_agent,
        "log",
        SimpleNamespace(
            info=lambda event, **kw: events.append({"event": event, **kw}),
            warning=lambda *a, **k: None,
        ),
    )
    res = await ask_agent.run_freeform("what should I have for dinner tonight?")
    assert res.answer == ask_agent.OUT_OF_SCOPE_ANSWER
    assert events and events[0]["event"] == "ask.freeform_unrouted"
    assert "dinner" in events[0]["question"]
