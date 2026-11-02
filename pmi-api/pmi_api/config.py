"""pmi-api settings layered on top of pmi-core's DB config."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    require_auth: bool = Field(default=True)
    cors_origins: str = Field(default="")  # comma-separated; "" = no CORS
    port: int = Field(default=8000)
    # Client copy served by GET /settings (legacy war-index /api/settings).
    future_phrase: str = Field(default="within the next 12 months")
    # Ask Micah (AI-native): the agent gathers each preset's data through
    # pmi-mcp on a fixed per-preset tool plan (ask_agent.PLANS) and has this
    # model write the answer. ONE env flips the provider:
    #   PMI_API_ASK_MODEL=local    → ask_local_model  (default; $0 box GPU)
    #   PMI_API_ASK_MODEL=chatgpt  → ask_openai_model (hosted OpenAI, billed)
    # Any other value is used verbatim: an ollama/-prefixed id stays local,
    # anything else goes to the OpenAI-compatible endpoint. Tool choice is
    # never delegated to the model either way (gemma3 can't tool-call).
    ask_model: str = Field(default="local")
    # qwen3:8b since 2026-08-29: same-question A/B against gemma3:4b
    # showed it stops the unit errors (seats quoted as "%") and invented
    # causality at ~+10s latency; served think-free via ollama's native
    # /api/chat (ask_agent._chat_request).
    ask_local_model: str = Field(default="ollama/qwen3:8b")
    ask_openai_model: str = Field(default="gpt-4o-mini-2024-07-18")
    # Free-form asking (v4): ON by default; a kill-switch
    # (PMI_API_ASK_FREEFORM_ENABLED=false), not the access control — the daily
    # quotas below are.
    ask_freeform_enabled: bool = Field(default=True)
    ask_mcp_url: str = Field(default="http://pmi-mcp:8100/mcp")
    ask_cache_ttl_s: int = Field(default=300)
    ask_max_tokens: int = Field(default=700)
    # Daily caps on answered /ask questions. Counts answered requests — presets
    # and routed free-form, cached or not; the disabled notice / out-of-scope
    # pointer / pipeline errors are free. Identities come from the pmi-web
    # proxy (service-key gated): `anon` = its signed micah_anon cookie, `user` =
    # the signed-in Google account.
    #   PMI_API_ASK_ANON_DAILY_QUOTA (5): per anonymous browser per day. 0 turns
    #     anonymous asking off, i.e. back to a sign-in wall.
    #   PMI_API_ASK_USER_DAILY_QUOTA (10): per signed-in account per day, IN
    #     TOTAL with what the same browser asked anonymously that day. <=0 =
    #     unlimited for signed-in users.
    # Callers with neither identity (SSO-off dev, require_auth=false) stay
    # unmetered. Admin reset = delete the rows (core_ask_log).
    ask_anon_daily_quota: int = Field(default=5)
    ask_user_daily_quota: int = Field(default=10)
    # The day the quotas count in: resets at local midnight here (IANA name).
    ask_quota_timezone: str = Field(default="America/New_York")
    # Entitlement defaults — read ONLY through pmi_api.entitlements.resolve_limits()
    # (CORR-6.9 (1)); plans (CORR-8.8) will override them per account there.
    #   PMI_API_USER_KEY_RPM (60): requests per minute a user API key declares
    #     when its row carries no rate_limit_per_minute; <=0 = unlimited.
    #     Declared only until CORR-6.9 (3) adds the meter.
    #   PMI_API_USER_KEYS_MAX (3): self-service keys one account may hold
    #     (SHIP-2.3a / CORR-6.11).
    #   PMI_API_REFERENCE_MAX_DAYS (400): deepest history the reference layer
    #     serves per request (SHIP-5.1 /reference).
    user_key_rpm: int = Field(default=60)
    user_keys_max: int = Field(default=3)
    reference_max_days: int = Field(default=400)
    # Per-query budget for chart-history reads, enforced with SET LOCAL inside
    # a savepoint (pmi_api.db_guard). A trend line is decoration on its page:
    # when the data box's disk is saturated it is dropped after this long
    # instead of failing the whole response (page_cache.trend_series). <=0
    # disables.
    history_timeout_ms: int = Field(default=5000)
    # /markets/search result cache (pmi_api.search_cache). Within fresh_s a
    # term is served from memory; until stale_s it is still served at once
    # while one background query refreshes it. fresh_s <= 0 disables caching.
    # query_timeout_s bounds each search query (and the wait for an identical
    # in-flight one); past it the route answers 503 + Retry-After.
    search_cache_fresh_s: int = Field(default=600)
    search_cache_stale_s: int = Field(default=7200)
    search_cache_max_entries: int = Field(default=512)
    search_cache_refresh_concurrency: int = Field(default=2)
    search_query_timeout_s: int = Field(default=60)
    # Optional tier 2 for the search cache, shared by every API process and
    # kept across pmi-api restarts. Empty = in-process tier only. Production
    # points it at its own cache-only Redis (docker-compose.remote-db.yml).
    search_cache_redis_url: str = Field(default="")

    model_config = SettingsConfigDict(
        env_prefix="PMI_API_",
        # Probe order: cwd .env, service-root .env (legacy), platform-root .env
        # (the consolidated single source of truth at pmi_data_platform/.env).
        env_file=(
            ".env",
            Path(__file__).resolve().parents[1] / ".env",
            Path(__file__).resolve().parents[2] / ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def ask_model_resolved(self) -> str:
        """The ask model id after alias resolution (see ask_model comment)."""
        alias = self.ask_model.strip().lower()
        if alias in {"local", "ollama"}:
            return self.ask_local_model
        if alias in {"chatgpt", "openai", "gpt"}:
            return self.ask_openai_model
        return self.ask_model

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


api_settings = ApiSettings()
