"""Top-level pipeline: load IR → select markets → factor eval → aggregate → write score.

End-to-end runnable from CLI:

    pmi-core pipeline run --index polymarket-war-index

Sprint 1/2 deliverable. Writes one `ts_index_scores` row + N `audit_evaluations`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import BigInteger, func, literal, select, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from pmi_core import mlflow_client
from pmi_core.config import settings
from pmi_core.db import session_scope
from pmi_core.dsl.ir import IndexDef, SemanticSelector, load_index_def
from pmi_core.engine.aggregator import MarketEvaluations, aggregate
from pmi_core.engine.factor_evaluator import (
    _is_stub,
    persist_evaluation,
    run_factor_llm,
    run_factor_llm_batch,
)
from pmi_core.engine.factor_resolver import ResolvedFactorModel, resolve_factor_model
from pmi_core.engine.selector import _race_tag_office, select_markets
from pmi_core.engine.tick_guard import TickGuardVerdict, partial_tick_verdict
from pmi_core.llm import embed_query
from pmi_core.llm.base import ParseError
from pmi_core.models import (
    AuditEvaluation,
    AuditPipelineRun,
    CoreIndexDefinition,
    CoreMarket,
    CoreMarketRaceTag,
    CorePrompt,
    TsIndexScore,
    TsOrderbookSnapshot,
    TsPriceSnapshot,
)
from pmi_core.vectorstore import get_vector_store

log = structlog.get_logger(__name__)

INDEX_DEFS_DIR = Path(__file__).resolve().parent.parent / "index_defs"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


async def _load_race_tags(
    session: AsyncSession, ir: IndexDef, market_ids: list[int]
) -> dict[int, CoreMarketRaceTag] | None:
    """market_id → race tag for a cycle-scoped US election index, else None.

    None keeps the aggregator on the keyword/title path; a dict switches the two
    election formulas to classify from the persisted tags (single source, no
    title re-parse). The selector already restricted membership to this office +
    cycle, so a plain load by id is enough.
    """
    if _race_tag_office(ir) is None or not market_ids:
        return None
    rows = (
        await session.execute(
            select(CoreMarketRaceTag).where(CoreMarketRaceTag.market_id.in_(market_ids))
        )
    ).scalars().all()
    return {t.market_id: t for t in rows}


async def ensure_index_definition(
    session: AsyncSession, ir: IndexDef, yaml_text: str, sha256: str
) -> CoreIndexDefinition:
    """Idempotent UPSERT-by-version. Bumping IR.version retires the previous current.

    Public (CORR-3.2): the CLI ``seed`` path and any caller that needs to
    register a definition before scoring imports this, so it must not carry a
    leading underscore.
    """
    existing = (
        await session.execute(
            select(CoreIndexDefinition).where(
                CoreIndexDefinition.index_id == ir.id,
                CoreIndexDefinition.version == ir.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.yaml_sha256 != sha256:
            raise RuntimeError(
                f"index_def {ir.id} v{ir.version} on disk diverged from DB (sha256 mismatch). "
                "Bump version instead of editing in place — SCD Type 2 contract."
            )
        return existing

    # Retire any older currently-active row for the same index_id (SCD Type 2).
    now = datetime.now(UTC)
    prev = (
        await session.execute(
            select(CoreIndexDefinition)
            .where(
                CoreIndexDefinition.index_id == ir.id,
                CoreIndexDefinition.is_current.is_(True),
            )
        )
    ).scalars().all()
    for p in prev:
        p.is_current = False
        p.effective_to = now

    # Open / look up a single MLflow experiment per index_id. NULL on failure.
    experiment_id = mlflow_client.ensure_experiment(ir.id)

    row = CoreIndexDefinition(
        index_id=ir.id,
        version=ir.version,
        title=ir.title,
        owner=ir.owner,
        definition=ir.model_dump(mode="json"),
        yaml_source=yaml_text,
        yaml_sha256=sha256,
        is_current=True,
        mlflow_experiment_id=experiment_id,
        effective_from=now,
    )
    session.add(row)
    await session.flush()
    return row


async def ensure_prompt(session: AsyncSession, factor_prompt_ref: str) -> CorePrompt:
    """Load `prompts/factors/<name>-vN.md`, register or fetch the matching CorePrompt row.

    Public (CORR-3.2): imported by the CLI ``prompts ensure`` path, the
    ``llm-batch`` job, and :func:`resolve_factor_models`.
    """
    if not factor_prompt_ref.startswith("prompts/"):
        raise ValueError(f"prompt_ref must start with 'prompts/': {factor_prompt_ref}")
    rel = factor_prompt_ref.removeprefix("prompts/")
    path = PROMPTS_DIR / f"{rel}.md"
    template = path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(template.encode("utf-8")).hexdigest()

    # Pull name + version from filename: "factors/direction-v4" → name=factors/direction, version=4
    if "-v" not in rel:
        raise ValueError(f"prompt_ref must end with -vN: {factor_prompt_ref}")
    name_part, version_part = rel.rsplit("-v", 1)
    version = int(version_part)

    existing = await session.execute(
        select(CorePrompt).where(
            CorePrompt.name == name_part,
            CorePrompt.version == version,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.sha256 != sha256:
            raise RuntimeError(
                f"Prompt {factor_prompt_ref} on disk diverged from DB (sha256 mismatch). "
                f"Bump version instead of editing in place."
            )
        return row

    prompt_uri = mlflow_client.register_prompt(
        name=name_part,
        template=template,
        sha256=sha256,
        tags={"version": str(version), "source": "git"},
    )
    row = CorePrompt(
        name=name_part,
        version=version,
        template=template,
        sha256=sha256,
        mlflow_prompt_uri=prompt_uri,
    )
    session.add(row)
    await session.flush()
    return row


async def resolve_factor_models(
    session: AsyncSession, ir: IndexDef
) -> tuple[dict[str, CorePrompt], dict[str, ResolvedFactorModel]]:
    """For each factor in `ir`: load its YAML prompt row + resolve the active
    (registry-or-YAML) model bundle.

    Public + shared (CORR-3.2 / CORR-1.10): the live pipeline and the backtest
    replay both call this so they agree on exactly which
    ``(prompt_sha256, model_id)`` identifies a reusable evaluation. Returns
    ``(prompt_rows, resolved_models)``, both keyed by ``factor.id``.
    """
    prompt_rows: dict[str, CorePrompt] = {}
    resolved_models: dict[str, ResolvedFactorModel] = {}
    for factor in ir.factors:
        yaml_prompt = await ensure_prompt(session, factor.prompt_ref)
        prompt_rows[factor.id] = yaml_prompt
        resolved_models[factor.id] = await resolve_factor_model(
            session, factor, yaml_prompt
        )
    return prompt_rows, resolved_models


async def _latest_prices(
    session: AsyncSession, market_ids: list[int]
) -> tuple[dict[int, float], dict[int, float | None]]:
    """Return the most recent (last_price, volume_24h) per market.

    Volume is the CORR-3.4 cold-start fallback for liquidity weighting —
    used by the aggregator only when no ``ts_orderbook_snapshots`` row
    exists for the market yet (e.g. a freshly ingested market that the
    CLOB poller hasn't picked up in its 60s cycle).
    """
    if not market_ids:
        return {}, {}
    # Read the denormalized `core_markets.last_price` / `last_volume_24h` pair
    # that the alembic-0013 AFTER-INSERT trigger keeps in sync with the freshest
    # priced ts_price_snapshots row — which is exactly what this function used to
    # go and re-derive. The old plan was a LATERAL ... ORDER BY snapshot_at DESC
    # LIMIT 1 probe per market: index-assisted, but every probe is a random touch
    # somewhere in a 67GB table, so a single tick scattered ~24k buffer reads over
    # cold storage (measured mean 34s/call in pg_stat_statements). core_markets is
    # already resident here because the selector just read these same rows.
    # `last_price IS NOT NULL` preserves the old skip-to-first-priced semantics.
    stmt = select(
        CoreMarket.id, CoreMarket.last_price, CoreMarket.last_volume_24h
    ).where(CoreMarket.id.in_(market_ids), CoreMarket.last_price.is_not(None))
    result = (await session.execute(stmt)).all()
    prices: dict[int, float] = {}
    volumes: dict[int, float | None] = {}
    for mid, price, vol in result:
        prices[mid] = float(price)
        volumes[mid] = float(vol) if vol is not None else None
    return prices, volumes


async def _latest_orderbook_depths(
    session: AsyncSession, market_ids: list[int]
) -> dict[int, float]:
    """Return the freshest ``bid_depth_1pct + ask_depth_1pct`` per market.

    Uses two-sided depth (CORR-3.4) so one-sided books — common for
    illiquid sides of binary markets — don't pretend to be deep. Markets
    with no orderbook snapshot are simply absent from the returned dict
    so the caller can fall back to ``volume_24h``.
    """
    if not market_ids:
        return {}
    # One index backward-scan per market (LATERAL ... ORDER BY snapshot_at DESC
    # LIMIT 1) fetches only the freshest snapshot. The old plan streamed every
    # historical row for hundreds of high-frequency real-money markets out of the
    # ~70GB / 62M-row table and deduped in Python (seq scan, tens of minutes); a
    # plain DISTINCT ON still sorts all rows because the index is snapshot_at ASC.
    mids = (
        func.unnest(literal(market_ids, type_=ARRAY(BigInteger)))
        .table_valued("id")
        .render_derived(with_types=False)
    )
    latest = (
        select(
            TsOrderbookSnapshot.bid_depth_1pct,
            TsOrderbookSnapshot.ask_depth_1pct,
        )
        .where(TsOrderbookSnapshot.market_id == mids.c.id)
        .order_by(TsOrderbookSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("ob")
    )
    stmt = select(
        mids.c.id, latest.c.bid_depth_1pct, latest.c.ask_depth_1pct
    ).select_from(mids.join(latest, true()))
    result = (await session.execute(stmt)).all()
    out: dict[int, float] = {}
    for mid, bid, ask in result:
        total = (float(bid) if bid is not None else 0.0) + (
            float(ask) if ask is not None else 0.0
        )
        if total > 0:
            out[mid] = total
    return out


async def _tier0_prefilter(markets: list, ir: IndexDef) -> tuple[list, dict]:
    """Drop candidates whose cosine to the index anchor(s) is below the Tier 0 floor.

    The cheap embedding gate that sits between selection and the (expensive)
    factor LLM loop. It uses the SemanticSelector anchors as the relevance
    reference — so an index gets Tier 0 culling for free by declaring at least
    one `semantic` selector, even if it ALSO selects by keyword/category. An
    index with no anchor has no embedding reference, so the gate is a no-op.

    Policy decisions:
      * **max over anchors** — a market relevant to ANY anchor survives.
      * **fail-open on missing embeddings** — a market with no vector row (writer
        hasn't run, or it's brand new) is KEPT, never silently dropped. Better to
        spend a factor call than to vanish a real market from the index.
      * **fail-open on embed errors** — if the anchor can't be embedded (endpoint
        down), the gate is skipped entirely for this tick.

    Returns `(survivors, stats)` where stats feeds the run log / summary.
    """
    stats = {
        "considered": len(markets),
        "dropped": 0,
        "no_embedding": 0,
        "dropped_by_cap": 0,
        "enabled": False,
    }
    anchors = [s.anchor for s in ir.selectors if isinstance(s, SemanticSelector)]
    if not anchors or not markets:
        return markets, stats

    model = settings.active_embedding_model
    floor = settings.embedding_tier0_min_cosine
    cap = settings.embedding_tier0_max_survivors
    store = get_vector_store()
    market_ids = [m.id for m in markets]

    best: dict[int, float] = {}
    try:
        for anchor in anchors:
            anchor_vec = await embed_query(anchor, model=model)
            sims = await store.cosine_for_markets(
                market_ids=market_ids, query_embedding=anchor_vec, model=model
            )
            for mid, cos in sims.items():
                if mid not in best or cos > best[mid]:
                    best[mid] = cos
    except Exception as exc:  # noqa: BLE001 - graceful degradation by design
        log.warning("tier0.skipped_embed_error", index_id=ir.id, error=str(exc))
        return markets, stats

    stats["enabled"] = True
    # Split survivors: cosine-scored (rankable, capped) vs no-embedding
    # (fail-open, always kept — can't be ranked, negligible at >98% coverage).
    scored: list[tuple[float, object]] = []
    fail_open: list[object] = []
    for m in markets:
        cos = best.get(m.id)
        if cos is None:
            stats["no_embedding"] += 1
            fail_open.append(m)
        elif cos >= floor:
            scored.append((cos, m))
        else:
            stats["dropped"] += 1

    # Cost backstop: keep only the top-`cap` by cosine. Bounds the factor LLM
    # loop on a single GPU when a cross-venue selection floods Tier 0 with
    # near-floor matches the cosine cut alone wouldn't thin enough.
    if cap is not None and len(scored) > cap:
        scored.sort(key=lambda t: t[0], reverse=True)
        stats["dropped_by_cap"] = len(scored) - cap
        scored = scored[:cap]

    survivors = [m for _, m in scored] + fail_open
    log.info(
        "tier0.prefilter",
        index_id=ir.id,
        floor=floor,
        cap=cap,
        kept=len(survivors),
        dropped=stats["dropped"],
        dropped_by_cap=stats["dropped_by_cap"],
        no_embedding=stats["no_embedding"],
    )
    return survivors, stats


async def load_cached_evaluations(
    session: AsyncSession,
    ir: IndexDef,
    resolved_models: dict[str, ResolvedFactorModel],
    market_ids: list[int],
) -> dict[tuple[int, str], AuditEvaluation]:
    """Stage 1 of T1: one IN-query per factor instead of one SELECT per
    (market, factor). Returns `{(market_id, factor_id): AuditEvaluation}` for
    judgments already in `audit_evaluations`.

    Public (CORR-3.2): imported by the ``llm-batch`` job and the backtest
    replay (CORR-1.10), which reuses this exact lookup so a version bump that
    doesn't touch factors/prompts/models replays on the prior version's evals.

    INDEX-INDEPENDENT cache: the lookup keys on
    ``(market, factor_id, prompt_sha256, model_id)`` and deliberately does NOT
    filter on ``index_definition_id``. A factor judgment is a pure function of the
    market text + prompt + model — it never depends on which index asked — so a
    market judged once is reused across index VERSIONS and across any index sharing
    the same factor+prompt+model, instead of being re-judged (and re-billed by the
    LLM) on every version bump. Previously the key included ``index_definition_id``,
    so e.g. bumping btc-bull-bear-index v1→v2→v3 re-judged the same markets three
    times. The reused row keeps its original ``index_definition_id`` as first-seen
    provenance; the judgment (prompt_sha + model + value) is identical, so score
    lineage and reproducibility are unaffected. ``order_by(id)`` makes the choice
    deterministic (the canonical earliest eval) when several indexes already judged
    the same market.
    """
    existing: dict[tuple[int, str], AuditEvaluation] = {}
    if not market_ids:
        return existing
    # When Tier 2 escalation is configured, prefer an escalated row over the
    # Tier 1 row for the same (market, factor) — query tier2 first so the
    # second (tier1) pass only fills the gaps.
    model_ids_by_pref: list[str | None] = []
    if settings.tier2_model_id:
        model_ids_by_pref.append(settings.tier2_model_id)
    model_ids_by_pref.append(None)  # None → the factor's own resolved model
    for factor in ir.factors:
        resolved = resolved_models[factor.id]
        for pref_model in model_ids_by_pref:
            model_id = pref_model or resolved.llm_model_id
            rows = (
                await session.execute(
                    select(AuditEvaluation)
                    .where(
                        AuditEvaluation.factor_id == factor.id,
                        AuditEvaluation.prompt_sha256 == resolved.prompt.sha256,
                        AuditEvaluation.model_id == model_id,
                        AuditEvaluation.market_id.in_(market_ids),
                    )
                    .order_by(AuditEvaluation.id)
                )
            ).scalars().all()
            for row in rows:
                # A stub is a FAILED eval (LLM unavailable / circuit open), not a
                # judgment — never serve it as a cache hit, or one tick's outage
                # gets frozen in forever. Skip it so the market re-evaluates for
                # real next tick (once the underlying cause — e.g. a dead API key
                # tripping the shared circuit breaker — is fixed).
                mr = row.model_response
                if isinstance(mr, dict) and mr.get("stub"):
                    continue
                existing.setdefault((row.market_id, factor.id), row)
    return existing


class _LLMGuard:
    """Per-tick budget cap (CORR-5.4) + consecutive-failure circuit breaker
    (CORR-0.5). Shared by all concurrent LLM calls of one tick; mutations are
    safe because asyncio tasks interleave only at awaits. Approximate under
    concurrency: calls already in flight when the guard trips still complete.
    """

    def __init__(self) -> None:
        self.spent_usd = 0.0
        self.consecutive_failures = 0
        self.open_reason: str | None = None  # 'budget_exceeded' | 'circuit_open'

    def check(self) -> str | None:
        return self.open_reason

    def record_success(self, cost_usd: float) -> None:
        self.consecutive_failures = 0
        self.spent_usd += cost_usd or 0.0
        budget = settings.llm_budget_usd_per_tick
        if budget > 0 and self.spent_usd >= budget and self.open_reason is None:
            self.open_reason = "budget_exceeded"
            log.warning("llm_guard.budget_exceeded", spent_usd=self.spent_usd, budget=budget)

    def record_failure(self, exc: BaseException | None = None) -> None:
        """Count a failed call toward the breaker — unless it's a `ParseError`.

        The breaker exists to stop hammering an *unavailable* provider (dead key,
        outage), which is what its `open_reason` reports. A `ParseError` is the
        opposite signal: the provider answered, and one market's JSON was
        unusable — small local models emit a bare `{}` often enough that a
        handful in a row is routine. Letting those trip the breaker converts a
        few unlucky markets into a tick-wide LLM blackout, and that costs twice:
        the rest of the tick scores from stubs, and stub rows are deliberately
        not cache-reusable (see `_load_existing_evaluations`), so the next tick
        re-pays every call the blackout skipped. Ticks then never build a cache.
        """
        if isinstance(exc, ParseError):
            return
        self.consecutive_failures += 1
        limit = settings.llm_circuit_breaker_failures
        if limit > 0 and self.consecutive_failures >= limit and self.open_reason is None:
            self.open_reason = "circuit_open"
            log.warning("llm_guard.circuit_open", consecutive_failures=self.consecutive_failures)


# Approx char budget for the per-market blocks in one batch prompt. gemma3's
# default ollama context is 4096 tokens; reserving ~400 for the rubric + ~400
# for the output array leaves ~3200 tokens ≈ ~12800 chars for market data. We
# target ~8000 to stay clear of truncation (a too-long batch truncates the
# output → count mismatch → `parse_factor_batch` raises → per-market fallback,
# so this is an efficiency heuristic, not a correctness requirement).
_BATCH_CHAR_BUDGET = 8000


def _batch_chunks(markets: list, k: int) -> list[list]:
    """Pack markets into chunks bounded by BOTH `k` and `_BATCH_CHAR_BUDGET`
    (estimated from title+description length). A single market exceeding the
    budget still gets its own (size-1) chunk — it just won't share a call.
    """
    chunks: list[list] = []
    cur: list = []
    cur_chars = 0
    for m in markets:
        size = len(m.title or "") + len(m.description or "")
        if cur and (len(cur) >= k or cur_chars + size > _BATCH_CHAR_BUDGET):
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(m)
        cur_chars += size
    if cur:
        chunks.append(cur)
    return chunks


async def _maybe_escalate(market, factor, resolved: ResolvedFactorModel, resp, guard: _LLMGuard) -> None:
    """CORR-5.7: if Tier 2 is configured and `resp` is under the confidence
    floor, run the Tier 2 model and attach it to `resp.extras["escalation"]`
    (in place) for the persist stage. Best-effort; shared by single + batch
    Stage-3 paths. No-op when `tier2_model_id` is unset (the default).
    """
    tier2 = settings.tier2_model_id
    if not (
        tier2
        and tier2 != resolved.llm_model_id
        and resp is not None
        and resp.confidence is not None
        and resp.confidence < settings.tier2_escalation_confidence
    ):
        return
    try:
        t2_resolved = dataclasses.replace(resolved, llm_model_id=tier2)
        t2_resp = await run_factor_llm(market, factor, t2_resolved)
        guard.record_success(t2_resp.cost_usd or 0.0)
        resp.extras = dict(resp.extras or {})
        resp.extras["escalation"] = {
            "tier2_model_id": tier2,
            "tier1_confidence": resp.confidence,
            "response": t2_resp,
        }
        log.info(
            "tier2.escalated",
            factor_id=factor.id,
            market_id=market.id,
            tier1_confidence=resp.confidence,
            tier2_model=tier2,
        )
    except Exception as exc:  # noqa: BLE001 - escalation is best-effort
        guard.record_failure(exc)
        log.warning(
            "tier2.escalation_failed",
            factor_id=factor.id,
            market_id=market.id,
            error=str(exc)[:200],
        )


async def _llm_under_sem(
    sem: asyncio.Semaphore,
    market,
    factor,
    resolved: ResolvedFactorModel,
    guard: _LLMGuard,
) -> tuple[tuple[int, str], object | None, str | None, int]:
    """Stage 3 of T1: one concurrent LLM call. Pure async HTTP, no session.

    Returns `((market_id, factor_id), llm_response|None, error|None, latency_ms)`.
    Exceptions are captured (not raised) so one bad call can't sink the gather —
    the persist stage turns a captured error into a deterministic stub fallback.
    """
    async with sem:
        reason = guard.check()
        if reason is not None:
            return (market.id, factor.id), None, reason, 0
        t0 = time.perf_counter()
        try:
            resp = await run_factor_llm(market, factor, resolved)
        except Exception as exc:  # noqa: BLE001 - fallback-to-stub by design
            guard.record_failure(exc)
            err = f"{type(exc).__name__}: {exc}"
            return (market.id, factor.id), None, err, int((time.perf_counter() - t0) * 1000)
        guard.record_success(resp.cost_usd or 0.0)
        await _maybe_escalate(market, factor, resolved, resp, guard)
        return (market.id, factor.id), resp, None, int((time.perf_counter() - t0) * 1000)


async def _llm_batch_under_sem(
    sem: asyncio.Semaphore,
    markets: list,
    factor,
    resolved: ResolvedFactorModel,
    guard: _LLMGuard,
) -> list[tuple[tuple[int, str], object | None, str | None, int]]:
    """Batch variant of `_llm_under_sem`: evaluate `factor` across N markets in
    one LLM call (rubric sent once). Returns one result tuple per market in the
    SAME shape as `_llm_under_sem`, so Stage 4 (persist) is unchanged.

    `run_factor_llm_batch` already falls back to per-market calls internally if
    batching isn't applicable or the response misaligns — so a returned resp is
    always a real, aligned per-market evaluation. Tier 2 escalation runs
    per-market afterwards (only fires when configured).
    """
    async with sem:
        reason = guard.check()
        if reason is not None:
            return [((m.id, factor.id), None, reason, 0) for m in markets]
        t0 = time.perf_counter()
        try:
            by_mid = await run_factor_llm_batch(markets, factor, resolved)
        except Exception as exc:  # noqa: BLE001 - fallback-to-stub by design
            guard.record_failure(exc)
            err = f"{type(exc).__name__}: {exc}"
            lat = int((time.perf_counter() - t0) * 1000)
            return [((m.id, factor.id), None, err, lat) for m in markets]
        lat = int((time.perf_counter() - t0) * 1000)
        out: list[tuple[tuple[int, str], object | None, str | None, int]] = []
        for m in markets:
            resp = by_mid.get(m.id)
            if resp is None:
                out.append(((m.id, factor.id), None, "batch returned no row for market", lat))
                continue
            guard.record_success(resp.cost_usd or 0.0)
            await _maybe_escalate(m, factor, resolved, resp, guard)
            out.append(((m.id, factor.id), resp, None, lat))
        return out


def _detect_disagreements(market_rows: list[MarketEvaluations], ir: IndexDef) -> dict:
    """CORR-5.8: flag markets whose weighted binary factors confidently
    disagree (some say 1, some say 0, both sides confidence ≥ 0.6) — e.g.
    `directly_about_war=1` but `armed_conflict=0`. Pure post-hoc analysis;
    feeds `ts_index_scores.breakdown.disagreement` + a log line.
    """
    weighted_binary = {f.id for f in ir.factors if f.weight is not None and f.type == "binary"}
    flagged: list[dict] = []
    for row in market_rows:
        ones, zeros = [], []
        for fid in weighted_binary:
            ev = row.by_factor.get(fid)
            if ev is None or ev.value_numeric is None:
                continue
            conf = float(ev.confidence) if ev.confidence is not None else 0.0
            if conf < 0.6:
                continue
            (ones if float(ev.value_numeric) >= 0.5 else zeros).append(fid)
        if ones and zeros:
            flagged.append({"market_id": row.market.id, "ones": sorted(ones), "zeros": sorted(zeros)})
    return {
        "markets_flagged": len(flagged),
        "examples": flagged[:5],
    }


async def _prices_as_of(
    session: AsyncSession, market_ids: list[int], as_of: datetime
) -> dict[int, float]:
    """Freshest priced snapshot at-or-before ``as_of`` per market — the price the
    market showed at that historical instant (for re-scoring past ticks)."""
    if not market_ids:
        return {}
    mids = (
        func.unnest(literal(market_ids, type_=ARRAY(BigInteger)))
        .table_valued("id")
        .render_derived(with_types=False)
    )
    latest = (
        select(TsPriceSnapshot.last_price)
        .where(
            TsPriceSnapshot.market_id == mids.c.id,
            TsPriceSnapshot.last_price.is_not(None),
            TsPriceSnapshot.snapshot_at <= as_of,
        )
        .order_by(TsPriceSnapshot.snapshot_at.desc())
        .limit(1)
        .lateral("ps")
    )
    stmt = select(mids.c.id, latest.c.last_price).select_from(mids.join(latest, true()))
    return {mid: float(p) for mid, p in (await session.execute(stmt)).all()}


async def backfill_scores(
    index_id: str,
    since: datetime,
    until: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Re-score the EXISTING ts_index_scores points in [since, until] IN PLACE.

    Each stored point keeps ``component_evaluation_ids`` — the exact evals that
    were its components at that tick. Backfill reloads those (so historical
    membership is faithful), re-prices each market as of the point's timestamp
    (``_prices_as_of``), and reruns the shared ``aggregate`` — nothing else. It
    does NOT re-run the LLM factors or re-select markets, so the ONLY thing that
    moves is the aggregator logic itself. Generic across every vertical. Use it
    after a scoring-logic change to correct stale historical points instead of
    waiting for them to age out of the chart window. Liquidity weighting uses the
    latest depth/volume as an approximation (historical depth is not replayed).
    Only points of the CURRENT index definition version are touched."""
    until = until or datetime.now(UTC)
    yaml_path = INDEX_DEFS_DIR / f"{index_id}.yaml"
    ir, _yaml_text, _sha = load_index_def(yaml_path)

    async with session_scope() as session:
        index_def_row = (
            await session.execute(
                select(CoreIndexDefinition)
                .where(CoreIndexDefinition.index_id == index_id)
                .order_by(CoreIndexDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if index_def_row is None:
            return {"index_id": index_id, "points": 0, "error": "no index definition"}

        points = (
            await session.execute(
                select(TsIndexScore)
                .where(
                    TsIndexScore.index_definition_id == index_def_row.id,
                    TsIndexScore.as_of >= since,
                    TsIndexScore.as_of <= until,
                )
                .order_by(TsIndexScore.as_of)
            )
        ).scalars().all()

        # Load every point's component evals once, keyed by eval id.
        all_eval_ids: set[int] = set()
        for row in points:
            all_eval_ids.update(row.component_evaluation_ids or [])
        evals_by_id: dict[int, AuditEvaluation] = {}
        if all_eval_ids:
            evals_by_id = {
                e.id: e
                for e in (
                    await session.execute(
                        select(AuditEvaluation).where(
                            AuditEvaluation.id.in_(list(all_eval_ids))
                        )
                    )
                ).scalars().all()
            }
        all_market_ids = {e.market_id for e in evals_by_id.values()}
        markets = (
            (
                await session.execute(
                    select(CoreMarket).where(CoreMarket.id.in_(list(all_market_ids)))
                )
            ).scalars().all()
            if all_market_ids
            else []
        )
        market_by_id = {m.id: m for m in markets}
        mid_list = list(all_market_ids)
        depths = await _latest_orderbook_depths(session, mid_list)
        _, volumes = await _latest_prices(session, mid_list)

        changes: list[dict] = []
        for row in points:
            by_market: dict[int, dict[str, AuditEvaluation]] = {}
            for eid in row.component_evaluation_ids or []:
                e = evals_by_id.get(eid)
                if e is not None:
                    by_market.setdefault(e.market_id, {})[e.factor_id] = e
            if not by_market:
                continue
            prices = await _prices_as_of(session, list(by_market), row.as_of)
            market_rows = [
                MarketEvaluations(
                    market=market_by_id[mid],
                    by_factor=by_market[mid],
                    last_price=prices.get(mid),
                    liquidity=(
                        depths.get(mid)
                        if depths.get(mid) is not None
                        else volumes.get(mid)
                    ),
                )
                for mid in by_market
                if mid in market_by_id
            ]
            race_tags = await _load_race_tags(
                session, ir, [r.market.id for r in market_rows]
            )
            result = aggregate(market_rows, ir, race_tags)
            old = float(row.score) if row.score is not None else None
            changes.append(
                {"as_of": row.as_of.isoformat(), "old": old, "new": result.score}
            )
            if not dry_run:
                row.score = result.score
                row.component_count = result.component_count

        if dry_run:
            await session.rollback()

    return {
        "index_id": index_id,
        "points": len(points),
        "dry_run": dry_run,
        "changes": changes,
    }


async def ensure_factor_evaluations(
    session: AsyncSession,
    ir: IndexDef,
    markets: list[CoreMarket],
    resolved_models: dict[str, ResolvedFactorModel],
    index_definition_id: int,
    *,
    factor_ids: set[str] | None = None,
) -> dict[tuple[int, str], AuditEvaluation]:
    """Evaluate + persist any *uncached* (market, factor) judgments, returning
    the full evalmap — for callers that need factor values but don't run the
    scoring pipeline (the ``tag-races`` classifier).

    Reuses the pipeline's own primitives (cache load → concurrent LLM →
    persist), so judgments are byte-identical to what ``run_pipeline`` writes
    (same prompt/model/cache key). The cache is index-independent (see
    :func:`load_cached_evaluations`), so an eval written here is reused by every
    index sharing that factor+prompt+model and by scoring later — never
    re-billed.

    Why it exists: since the race-tag migration, cycle-scoped election indexes
    select via ``_select_by_race_tag`` (already-tagged markets only), so scoring
    no longer evaluates the keyword pool — the pre-migration path that
    bootstrapped candidate-leg party (``republican_on_yes``) for every venue.
    A brand-new candidate market (a whole new venue like IBKR, or a fresh leg
    on an existing one) therefore had no way to get its first party judgment,
    so it never classified, never tagged, never scored. tag-races calls this to
    self-bootstrap the classification factors on its keyword pool.

    ``factor_ids`` limits work to a subset of ``ir.factors`` (the
    classification-relevant ones); None evaluates them all.
    """
    factors = [f for f in ir.factors if factor_ids is None or f.id in factor_ids]
    market_ids = [m.id for m in markets]
    evalmap = await load_cached_evaluations(session, ir, resolved_models, market_ids)
    todo = [(m, f) for m in markets for f in factors if (m.id, f.id) not in evalmap]
    real_todo = [
        (m, f) for (m, f) in todo if not _is_stub(resolved_models[f.id].llm_model_id)
    ]
    llm_results: dict[tuple[int, str], tuple[object | None, str | None, int]] = {}
    if real_todo:
        guard = _LLMGuard()
        sem = asyncio.Semaphore(settings.llm_concurrency)
        gathered = await asyncio.gather(
            *[_llm_under_sem(sem, m, f, resolved_models[f.id], guard) for (m, f) in real_todo]
        )
        for key, resp, err, lat in gathered:
            llm_results[key] = (resp, err, lat)
    for (m, f) in todo:
        resp, err, lat = llm_results.get((m.id, f.id), (None, None, 0))
        # Strip a Tier 2 escalation payload (non-serializable) as run_pipeline does;
        # Tier 2 is config-gated off, so this is defensive.
        if resp is not None and getattr(resp, "extras", None) and "escalation" in resp.extras:
            resp.extras.pop("escalation")
        row, _ = await persist_evaluation(
            session,
            m,
            f,
            index_definition_id,
            resolved_models[f.id],
            llm_response=resp,
            llm_error=err,
            latency_ms=lat,
        )
        evalmap[(m.id, f.id)] = row
    return evalmap


async def _tick_guard(
    session: AsyncSession,
    index_definition_id: int,
    as_of: datetime,
    component_count: int,
) -> TickGuardVerdict:
    """Partial-tick guard (engine/tick_guard.py): compare this tick's member
    count with the trailing window of THIS definition's published ticks.

    Baseline is per ``index_definition_id`` on purpose — a version bump that
    deliberately changes membership starts with no baseline and publishes;
    the guard only catches a collapse within one definition.
    """
    min_ratio = settings.tick_guard_min_ratio
    if min_ratio <= 0:
        return partial_tick_verdict(
            component_count, [], latest_published_at=None, now=as_of,
            min_ratio=0.0, min_baseline=0, max_hold_hours=0.0,
        )
    window_start = as_of - timedelta(hours=settings.tick_guard_window_hours)
    rows = (
        await session.execute(
            select(TsIndexScore.as_of, TsIndexScore.component_count)
            .where(
                TsIndexScore.index_definition_id == index_definition_id,
                TsIndexScore.as_of >= window_start,
                TsIndexScore.as_of < as_of,
            )
            .order_by(TsIndexScore.as_of.desc())
            .limit(200)
        )
    ).all()
    counts = [int(c) for _, c in rows if c is not None]
    latest = rows[0][0] if rows else None
    return partial_tick_verdict(
        component_count,
        counts,
        latest_published_at=latest,
        now=as_of,
        min_ratio=min_ratio,
        min_baseline=settings.tick_guard_min_baseline,
        max_hold_hours=settings.tick_guard_max_hold_hours,
    )


async def run_pipeline(index_id: str, as_of: datetime | None = None) -> dict:
    """Single tick of the pipeline. Returns a dict summary for CLI display.

    ``published`` in the summary is False when the partial-tick guard held the
    score (member set collapsed vs the trailing baseline): evaluations are
    still persisted (cache), the run row records the verdict, but no
    ``ts_index_scores`` row is written, so readers keep the last good level.
    """
    as_of = as_of or datetime.now(UTC)
    yaml_path = INDEX_DEFS_DIR / f"{index_id}.yaml"
    ir, yaml_text, sha256 = load_index_def(yaml_path)

    async with session_scope() as session:
        run = AuditPipelineRun(
            name=f"pipeline:{index_id}",
            started_at=as_of,
            status="running",
        )
        session.add(run)
        await session.flush()

        try:
            index_def_row = await ensure_index_definition(session, ir, yaml_text, sha256)
            run.index_definition_id = index_def_row.id

            markets = await select_markets(session, ir)
            log.info("pipeline.selected", index_id=index_id, count=len(markets))

            # Tier 0 embedding pre-filter: cull anchor-irrelevant candidates
            # (e.g. keyword false-positives) before spending factor LLM calls.
            markets, tier0_stats = await _tier0_prefilter(markets, ir)
            if tier0_stats["enabled"]:
                log.info("pipeline.tier0", index_id=index_id, **tier0_stats)

            # Cache prompt rows AND resolve the active (factor_model) per factor.
            # If any factor has a CoreFactorModel registered + is_active=True at the
            # default stage, the resolver swaps in its (prompt + llm + temp + tools).
            # Otherwise the YAML-loaded prompt + stub-deterministic-v1 wins.
            prompt_rows, resolved_models = await resolve_factor_models(session, ir)

            # Cost / counter roll-up for audit_pipeline_runs.
            # cache_hits  → existing rows, no new spend this tick.
            # llm_calls   → fresh writes where the real LLM actually responded
            #               (stub fallbacks on failure don't count — they cost $0
            #               and produced `stub=True` payloads).
            # cost_usd    → sum of fresh writes' cost_usd (cache hits excluded
            #               so reruns don't double-charge).
            # evaluations_written → fresh writes only, matching the column name.
            evaluations_written = 0
            cache_hits = 0
            llm_calls = 0
            cost_usd_total = 0.0
            market_rows: list[MarketEvaluations] = []
            market_ids = [m.id for m in markets]
            prices, volumes = await _latest_prices(session, market_ids)
            depths = await _latest_orderbook_depths(session, market_ids)

            # Parent MLflow run for this tick. Child runs (one per factor eval)
            # hang under it. NULL when MLflow is down — pipeline still proceeds.
            parent_run_name = f"tick:{index_id}@{as_of.isoformat(timespec='seconds')}"
            with mlflow_client.start_run(
                experiment_id=index_def_row.mlflow_experiment_id,
                run_name=parent_run_name,
                tags={
                    "index_id": index_id,
                    "index_version": str(index_def_row.version),
                    "index_definition_id": index_def_row.id,
                    "yaml_sha256": sha256,
                    "pipeline_run_id": run.id,
                },
            ) as parent_run_id:
                run.mlflow_run_id = parent_run_id
                mlflow_client.log_params(
                    parent_run_id,
                    {
                        "index_id": index_id,
                        "index_version": index_def_row.version,
                        "yaml_sha256": sha256,
                        "factor_count": len(ir.factors),
                        "min_components": ir.aggregation.min_components,
                        "collapse_enabled": ir.aggregation.collapse.enabled,
                    },
                )

                # ── T1: 4-stage concurrent factor eval ──────────────────────
                # Stage 1 — batch-load the cache (one IN-query per factor).
                evalmap = await load_cached_evaluations(
                    session, ir, resolved_models, market_ids
                )
                cache_hits += len(evalmap)

                # Stage 2 — todo = cache misses; split stub (CPU) vs real (LLM).
                todo = [
                    (m, f)
                    for m in markets
                    for f in ir.factors
                    if (m.id, f.id) not in evalmap
                ]
                real_todo = [
                    (m, f)
                    for (m, f) in todo
                    if not _is_stub(resolved_models[f.id].llm_model_id)
                ]

                # Stage 3 — concurrent LLM (the ONLY concurrent point). Pure
                # HTTP under a semaphore; no session is touched here.
                #
                # Batching (PMI_LLM_BATCH_MARKETS > 1): group cache-misses by
                # factor and send up to K markets per call so the factor rubric
                # is prefilled ONCE instead of once-per-market (~2x on a
                # bandwidth-bound T4 at K=8). The cache/audit layer is unchanged
                # — each market still yields its own audit row under the same
                # (template sha, model_id) key; batching is invisible below
                # Stage 3. `run_factor_llm_batch` falls back to per-market calls
                # on any misalignment, so a returned resp is always aligned.
                # K=1 → byte-identical to the pre-batch per-(m,f) path.
                llm_results: dict[tuple[int, str], tuple[object | None, str | None, int]] = {}
                guard = _LLMGuard()
                if real_todo:
                    sem = asyncio.Semaphore(settings.llm_concurrency)
                    k = max(1, settings.llm_batch_markets)
                    by_factor: dict[str, list] = {}
                    for (m, f) in real_todo:
                        by_factor.setdefault(f.id, []).append(m)
                    factor_by_id = {f.id: f for f in ir.factors}
                    coros = []
                    if k <= 1:
                        coros = [
                            _llm_under_sem(sem, m, f, resolved_models[f.id], guard)
                            for (m, f) in real_todo
                        ]
                    else:
                        for fid, fmarkets in by_factor.items():
                            f = factor_by_id[fid]
                            for chunk in _batch_chunks(fmarkets, k):
                                coros.append(
                                    _llm_batch_under_sem(
                                        sem, chunk, f, resolved_models[fid], guard
                                    )
                                )
                    gathered = await asyncio.gather(*coros)
                    for item in gathered:
                        # single path → one tuple; batch path → list of tuples.
                        rows = item if isinstance(item, list) else [item]
                        for key, resp, err, lat in rows:
                            llm_results[key] = (resp, err, lat)

                # Stage 4 — persist serially on the single session (AsyncSession
                # is not concurrency-safe). Stubs and LLM-failures fall back here.
                for (m, f) in todo:
                    resp, err, lat = llm_results.get((m.id, f.id), (None, None, 0))
                    # CORR-5.7: a Tier 2 escalation rides along in extras. Strip
                    # it from the Tier 1 payload (it holds a non-serializable
                    # LLMResponse) and persist it as its OWN audit row under the
                    # tier2 model's cache key — both rows are real audit facts.
                    escalation = None
                    if resp is not None and resp.extras and "escalation" in resp.extras:
                        escalation = resp.extras.pop("escalation")
                    row, conflict_hit = await persist_evaluation(
                        session,
                        m,
                        f,
                        index_def_row.id,
                        resolved_models[f.id],
                        llm_response=resp,
                        llm_error=err,
                        latency_ms=lat,
                        experiment_id=index_def_row.mlflow_experiment_id,
                        parent_run_id=parent_run_id,
                    )
                    evalmap[(m.id, f.id)] = row
                    if conflict_hit:
                        # A concurrent tick wrote this cache_key first.
                        cache_hits += 1
                    else:
                        evaluations_written += 1
                        # `model_response.stub` is False only when a real LLM
                        # call returned a parsed response; stub fallbacks on LLM
                        # failure keep stub=True.
                        payload = row.model_response or {}
                        if payload.get("stub") is False:
                            llm_calls += 1
                        if row.cost_usd is not None:
                            cost_usd_total += float(row.cost_usd)
                    if escalation is not None:
                        t2_resp = escalation["response"]
                        t2_resp.extras = dict(t2_resp.extras or {})
                        t2_resp.extras["escalated_from"] = {
                            "model_id": resolved_models[f.id].llm_model_id,
                            "tier1_confidence": escalation["tier1_confidence"],
                        }
                        t2_resolved = dataclasses.replace(
                            resolved_models[f.id],
                            llm_model_id=escalation["tier2_model_id"],
                        )
                        t2_row, t2_conflict = await persist_evaluation(
                            session,
                            m,
                            f,
                            index_def_row.id,
                            t2_resolved,
                            llm_response=t2_resp,
                            llm_error=None,
                            latency_ms=0,
                            experiment_id=index_def_row.mlflow_experiment_id,
                            parent_run_id=parent_run_id,
                        )
                        # Tier 2 row wins for aggregation this tick (and on
                        # future ticks via the loader's tier2-first preference).
                        evalmap[(m.id, f.id)] = t2_row
                        if not t2_conflict:
                            evaluations_written += 1
                            if (t2_row.model_response or {}).get("stub") is False:
                                llm_calls += 1
                            if t2_row.cost_usd is not None:
                                cost_usd_total += float(t2_row.cost_usd)

                # Assemble per-market evaluation bundles for the aggregator.
                for market in markets:
                    by_factor = {f.id: evalmap[(market.id, f.id)] for f in ir.factors}
                    # CORR-3.4: depth is the primary liquidity signal; volume_24h
                    # is the cold-start fallback. ``None`` flows through to the
                    # aggregator as "no signal → uniform weight", not a zero.
                    liquidity = depths.get(market.id)
                    if liquidity is None:
                        liquidity = volumes.get(market.id)
                    market_rows.append(
                        MarketEvaluations(
                            market=market,
                            by_factor=by_factor,
                            last_price=prices.get(market.id),
                            liquidity=liquidity,
                        )
                    )

                race_tags = await _load_race_tags(
                    session, ir, [m.id for m in markets]
                )
                result = aggregate(market_rows, ir, race_tags)

                # CORR-5.8: confident factor disagreement, recorded per tick.
                disagreement = _detect_disagreements(market_rows, ir)
                if isinstance(result.breakdown, dict):
                    result.breakdown["disagreement"] = disagreement
                    if guard.open_reason:
                        result.breakdown["llm_guard"] = {
                            "tripped": guard.open_reason,
                            "spent_usd": guard.spent_usd,
                            "consecutive_failures": guard.consecutive_failures,
                        }
                if disagreement["markets_flagged"]:
                    log.info(
                        "pipeline.disagreement",
                        index_id=index_id,
                        flagged=disagreement["markets_flagged"],
                    )

                # Partial-tick guard: a member set that collapsed vs this
                # definition's recent ticks is not a market observation and
                # must not become the headline (2026-10-06: 28 members → 57.1
                # against a ~180-member 45–48 baseline, served by every reader).
                verdict = await _tick_guard(
                    session, index_def_row.id, as_of, result.component_count
                )
                if verdict.partial:
                    log.warning(
                        "pipeline.partial_tick",
                        index_id=index_id,
                        markets_in=len(markets),
                        **verdict.to_dict(),
                    )
                    if isinstance(result.breakdown, dict):
                        result.breakdown["partial_tick"] = verdict.to_dict()

                if verdict.publish:
                    score_row = TsIndexScore(
                        index_definition_id=index_def_row.id,
                        as_of=as_of,
                        score=result.score,
                        component_count=result.component_count,
                        component_evaluation_ids=result.component_evaluation_ids,
                        breakdown=result.breakdown,
                    )
                    session.add(score_row)

                mlflow_client.log_metrics(
                    parent_run_id,
                    {
                        "score": result.score,
                        "component_count": result.component_count,
                        "markets_in": len(markets),
                        "evaluations_written": evaluations_written,
                        "cache_hits": cache_hits,
                        "llm_calls": llm_calls,
                        "cost_usd": cost_usd_total,
                        "published": int(verdict.publish),
                    },
                )

            run.ended_at = datetime.now(UTC)
            run.markets_in = len(markets)
            run.evaluations_written = evaluations_written
            run.scores_out = 1 if verdict.publish else 0
            run.llm_calls = llm_calls
            run.cost_usd = cost_usd_total
            run.status = "succeeded"
            if verdict.partial:
                run.metadata_json = {"partial_tick": verdict.to_dict()}

            log.info(
                "pipeline.done",
                index_id=index_id,
                markets=len(markets),
                evals=evaluations_written,
                cache_hits=cache_hits,
                llm_calls=llm_calls,
                cost_usd=cost_usd_total,
                score=result.score,
                component_count=result.component_count,
                published=verdict.publish,
                mlflow_run_id=run.mlflow_run_id,
            )

            return {
                "index_id": index_id,
                "index_definition_id": index_def_row.id,
                "as_of": as_of.isoformat(),
                "score": result.score,
                "component_count": result.component_count,
                "published": verdict.publish,
                "tick_guard": verdict.to_dict() if verdict.partial else None,
                "markets_in": len(markets),
                "evaluations_written": evaluations_written,
                "cache_hits": cache_hits,
                "llm_calls": llm_calls,
                "cost_usd": cost_usd_total,
                "breakdown": result.breakdown,
                "mlflow_run_id": run.mlflow_run_id,
                "mlflow_experiment_id": index_def_row.mlflow_experiment_id,
            }
        except Exception as exc:
            run.ended_at = datetime.now(UTC)
            run.status = "failed"
            run.error_message = repr(exc)
            log.error("pipeline.failed", index_id=index_id, error=str(exc))
            raise
