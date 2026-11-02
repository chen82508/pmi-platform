"""Pydantic settings loaded from env (PMI_* prefix) and / or .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_name: str = Field(default="pmi")
    db_user: str = Field(default="pmi")
    db_password: str = Field(default="pmi")
    db_ssl: bool = Field(default=False)

    # Use the conventional unprefixed `OPENAI_API_KEY` name (matches OpenAI's
    # SDK default and what `pmi_data_platform/.env` provides). Without this
    # alias, the `env_prefix="PMI_"` below would otherwise force callers to
    # use `PMI_OPENAI_API_KEY`, which would confuse anyone copying a key from
    # an `OPENAI_API_KEY=...` env block.
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")

    # Optional override of the LLM endpoint. Leave empty to hit OpenAI directly.
    # Set to an OpenAI-compatible server (vLLM / Ollama / TGI / a future
    # self-hosted ML server) to route real-LLM evaluations there instead — the
    # provider stays `OpenAIProvider`, only the transport target changes.
    # `PMI_LLM_API_KEY` is the bearer token for that endpoint; when empty we
    # fall back to `openai_api_key` so existing OpenAI configs keep working.
    llm_base_url: str = Field(default="")
    llm_api_key: str = Field(default="")

    # T1: max concurrent factor LLM calls per pipeline tick. The pipeline fans
    # the (market, factor) LLM calls out under an asyncio.Semaphore of this size,
    # then persists results serially (one AsyncSession is not concurrency-safe).
    # Match this to the serving capacity: for local Ollama, also raise the
    # ollama container's OLLAMA_NUM_PARALLEL or requests just queue server-side.
    llm_concurrency: int = Field(default=8)

    # Multi-market batching: how many markets to evaluate for ONE factor in a
    # single LLM call. The factor rubric (the bulk of the prompt) is then
    # prefilled once instead of once-per-market — ~2x throughput at 8 on a
    # bandwidth-bound T4. 1 = OFF (one call per (market, factor); byte-identical
    # to the pre-batch path). Only `binary`/`ternary`/`score` factors batch;
    # `extraction` and providers without `evaluate_batch` fall back to singles.
    # Cache/audit lineage is unaffected (key = template sha + model_id).
    llm_batch_markets: int = Field(default=1, ge=1)

    # CORR-5.4: hard cost ceiling per pipeline tick (USD). 0 = unlimited. Once
    # accumulated fresh-call cost crosses this, remaining LLM calls in the tick
    # short-circuit to the deterministic stub (fallback_reason=budget_exceeded).
    # Approximate under concurrency: calls already in flight still complete.
    llm_budget_usd_per_tick: float = Field(default=0.0)
    # CORR-0.5: account-wide circuit breaker. After this many CONSECUTIVE LLM
    # failures within a tick, stop calling the LLM for the rest of the tick
    # (remaining evals fall back to stub, fallback_reason=circuit_open).
    # 0 = disabled. Successes reset the streak.
    llm_circuit_breaker_failures: int = Field(default=8)

    # CORR-5.3: build + log the Batch API request file without uploading —
    # the only verifiable mode without an OpenAI key.
    batch_dry_run: bool = Field(default=False)

    # CORR-5.2 (Tier 3): re-score an index when a component market's price has
    # drifted ≥ this many probability POINTS (0-100 scale) vs ~lookback ago.
    # The factor cache is untouched (append-only invariant) — drift re-runs the
    # pipeline tick so prices/weights/aggregation refresh ahead of the hourly cron.
    drift_threshold_pct: float = Field(default=10.0)
    drift_lookback_hours: int = Field(default=24)

    # Partial-tick guard (engine/tick_guard.py). A tick whose component_count
    # falls below `min_ratio` × the median component_count of this index
    # definition's published ticks in the trailing `window_hours` is NOT written
    # to ts_index_scores (so no reader serves a collapsed member set as the
    # headline). Indexes whose baseline median is under `min_baseline` are not
    # guarded (ratios on a handful of members are noise). A hold expires once
    # the index has had no published score for `max_hold_hours`: the next tick
    # then publishes, flagged `breakdown.partial_tick`, so a genuinely shrunken
    # index does not freeze. min_ratio <= 0 disables the guard.
    tick_guard_min_ratio: float = Field(default=0.5)
    tick_guard_min_baseline: int = Field(default=20)
    tick_guard_window_hours: int = Field(default=24)
    tick_guard_max_hold_hours: float = Field(default=6.0)

    # CORR-5.7: Tier 1 → Tier 2 escalation. When a Tier 1 eval returns
    # confidence below this floor, re-run the factor on `tier2_model_id`
    # (e.g. "agentic/llama3.2" or a bigger ollama tag) and persist BOTH rows —
    # the Tier 2 row (its own cache key) is preferred at aggregation.
    # tier2_model_id empty = escalation off.
    tier2_model_id: str = Field(default="")
    tier2_escalation_confidence: float = Field(default=0.55)

    # Ollama (local model worker). Independent of `llm_base_url` so an Ollama
    # endpoint can coexist with OpenAI-direct: promote a CoreFactorModel with an
    # `ollama/<model>` model_id (e.g. `ollama/llama3.1`) to route it here.
    # Inside docker compose this is overridden to `http://ollama:11434/v1`.
    # The trailing `/v1` is Ollama's OpenAI-compatible API surface.
    ollama_base_url: str = Field(default="http://localhost:11434/v1")

    # ── Embeddings (Tier 0 pre-filter + SemanticSelector) ──────────────────
    # `active_embedding_model` is the single source of truth for "which model
    # the engine queries". It is config, NOT a column in vec_market_embeddings
    # (that table is row-per-model; storing the active model per-row would be
    # denormalised). The value carries a provider prefix so the embed path can
    # route it the same way `get_provider` routes chat models:
    #   ollama/<tag>            → OllamaProvider endpoint (PMI_OLLAMA_BASE_URL), free
    #   text-embedding-* / gpt* → OpenAI embeddings endpoint
    # `nomic-embed-text` needs asymmetric task prefixes (search_document: for
    # stored markets, search_query: for the anchor) — handled in llm/embeddings.
    active_embedding_model: str = Field(default="ollama/nomic-embed-text")

    # Tier 0 cosine floor: candidate markets whose cosine(anchor, market) falls
    # below this are skipped before the (expensive) factor LLM loop. A LOW floor
    # (recall-first) — precision is the factor evaluator's job downstream.
    embedding_tier0_min_cosine: float = Field(default=0.5)

    # Tier 0 cost backstop. Once venue is no longer a hard membership filter, a
    # keyword/semantic selection can pull in thousands of cross-venue candidates;
    # the cosine floor culls most, but on a single GPU the factor LLM loop must
    # stay bounded even when separation is weak. When set, Tier 0 keeps at most
    # this many cosine-scored survivors (the highest-cosine ones); markets with
    # no embedding are fail-open kept regardless (negligible at >98% coverage).
    # None = no cap (the floor alone gates) — keeps venue-scoped indexes
    # byte-identical. Tune per the cross-venue measurement.
    embedding_tier0_max_survivors: int | None = Field(default=None)

    # Which VectorStore implementation backs SemanticSelector / Tier 0 / the
    # writer. `pgvector` (default) keeps vectors in Postgres — zero new infra.
    # `milvus` is a forward-compat stub until scale justifies it (§3.1).
    vector_store: str = Field(default="pgvector")

    # CORR-2.6: global default cap on markets a selector returns per index.
    # Per-index override = `max_markets` in the index YAML. The selector logs
    # `selector.limit_saturated` when an index hits the cap.
    selector_max_markets: int = Field(default=500)

    # Venues the embed-markets job generates vectors for. Now that membership is
    # relevance-based across all venues, Tier 0 + SemanticSelector need vectors
    # for every venue an index might pull from — so the default covers all
    # ingested venues (was polymarket-only under CORR-3.12's venue-scoped model).
    embed_venues: list[str] = Field(
        default=["polymarket", "kalshi", "manifold", "forecastex", "predictit", "gemini", "robinhood"]
    )

    # MLflow tracking + Prompt Registry. Pipeline gracefully degrades if unreachable
    # or if PMI_MLFLOW_ENABLED=false — audit_evaluations remain the source of truth.
    mlflow_tracking_uri: str = Field(default="http://localhost:5500")
    mlflow_enabled: bool = Field(default=True)
    # Per-factor-eval MLflow child runs are a non-authoritative mirror (the
    # authoritative lineage is the append-only audit_evaluations row). Each child
    # run is several synchronous HTTP calls, which serialize in the persist stage
    # and dominate a big fresh score (T1). Set false to skip them for throughput
    # — `audit_evaluations.mlflow_run_id` is then NULL but the row is unchanged.
    # The parent pipeline run (audit_pipeline_runs) is unaffected either way.
    mlflow_factor_child_runs: bool = Field(default=True)
    mlflow_experiment_prefix: str = Field(default="pmi.")  # experiments named e.g. "pmi.polymarket-war-index"

    # ── Postgres job queue (CORR-4.6, Redis-free) ───────────────────────────
    # `pmi-workers worker` claim loop. Concurrency is jobs-in-flight per worker
    # process; run_pipeline is internally concurrent already, so keep this low.
    worker_concurrency: int = Field(default=2)
    # Fallback poll cadence when LISTEN/NOTIFY is unavailable (it normally
    # wakes the worker instantly on enqueue).
    worker_poll_interval_sec: float = Field(default=2.0)
    worker_heartbeat_sec: float = Field(default=15.0)
    # A 'running' job whose heartbeat is older than this is presumed crashed
    # (worker OOM / container kill) and re-queued — Temporal-style at-least-once.
    worker_stale_after_sec: float = Field(default=300.0)
    job_default_max_attempts: int = Field(default=3)
    # Retry delay = base × 2^(attempts-1), capped at 10 min.
    job_retry_backoff_base_sec: float = Field(default=10.0)

    # WS-triggered re-eval (CORR-4.6 on-demand path): skip re-scoring an index
    # whose latest score is fresher than this — trade storms then cost one
    # pipeline tick per index per interval, not one per trade.
    ws_reeval_min_interval_sec: int = Field(default=300)

    # ── Weekly report email (landing "Get our updates" subscription) ────────
    # SMTP relay for the `weekly-report` worker job. Empty host = email
    # delivery disabled — the job still builds the report and logs it, but
    # skips sending (graceful degradation, same philosophy as MLflow).
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_starttls: bool = Field(default=True)
    email_from: str = Field(default="Micah Indexes <updates@localhost>")
    # Browser-reachable pmi-web base URL, used for links inside the email
    # (index pages + the per-subscriber /unsubscribe?token=... footer link).
    public_web_url: str = Field(default="http://localhost:3030")

    log_level: str = Field(default="INFO")

    model_config = SettingsConfigDict(
        env_prefix="PMI_",
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
    def database_url(self) -> str:
        """Async SQLAlchemy URL."""
        ssl = "?ssl=require" if self.db_ssl else ""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}{ssl}"
        )

    @property
    def sync_database_url(self) -> str:
        """Sync URL used by Alembic."""
        ssl = "?sslmode=require" if self.db_ssl else ""
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}{ssl}"
        )


settings = Settings()
