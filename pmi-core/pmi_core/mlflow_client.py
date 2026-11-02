"""MLflow integration with graceful degradation.

Design contract:
    * `audit_evaluations`, `core_prompts`, `audit_pipeline_runs` in Postgres are the
      compliance-grade source of truth.
    * MLflow mirrors them for UI / search / Prompt Registry ergonomics.
    * If MLflow is unreachable, every helper here returns None / silently no-ops
      and the pipeline continues. Audit rows stay correct; their `mlflow_*`
      columns just stay NULL.

Operations exposed:
    ensure_experiment(name)         → experiment_id | None
    register_prompt(name, template) → prompt_uri    | None
    start_run(...)                   ctx → run_id   | None
    log_params(run_id, dict)
    log_metrics(run_id, dict)
    log_text(run_id, content, path)

All MLflow API access is centralised here. The rest of pmi-core never imports
`mlflow` directly — that keeps the graceful-degradation contract enforceable.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import structlog

from pmi_core.config import settings

log = structlog.get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Internal state — lazily initialised, soft-disable after first hard failure
# ───────────────────────────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client: Any | None = None
_mlflow_module: Any | None = None
_disabled = False


def _get_client() -> Any | None:
    """Return a cached MlflowClient or None if MLflow is unavailable / disabled."""
    global _client, _mlflow_module, _disabled

    if _disabled:
        return None
    if not settings.mlflow_enabled:
        return None
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client
        try:
            import mlflow as _mlflow
            from mlflow.tracking import MlflowClient

            _mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
            _client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri)
            _mlflow_module = _mlflow
            log.info("mlflow.ready", uri=settings.mlflow_tracking_uri)
        except Exception as exc:
            log.warning(
                "mlflow.unavailable",
                uri=settings.mlflow_tracking_uri,
                error=str(exc),
            )
            _disabled = True
            return None
    return _client


def is_enabled() -> bool:
    """Cheap probe — True if we expect future calls to succeed."""
    return _get_client() is not None


def reset_for_tests() -> None:
    """Force re-init on next access. Tests only."""
    global _client, _mlflow_module, _disabled
    with _client_lock:
        _client = None
        _mlflow_module = None
        _disabled = False


# ───────────────────────────────────────────────────────────────────────────
# Experiments — one per index_id (prefixed by settings.mlflow_experiment_prefix)
# ───────────────────────────────────────────────────────────────────────────


def ensure_experiment(index_id: str) -> str | None:
    """Return MLflow experiment_id for an index, creating it if missing."""
    client = _get_client()
    if client is None:
        return None

    full_name = f"{settings.mlflow_experiment_prefix}{index_id}"
    try:
        exp = client.get_experiment_by_name(full_name)
        if exp is not None:
            return exp.experiment_id
        return client.create_experiment(full_name)
    except Exception as exc:
        log.warning("mlflow.ensure_experiment_failed", index_id=index_id, error=str(exc))
        return None


# ───────────────────────────────────────────────────────────────────────────
# Prompt Registry — mirror of `core_prompts`
# ───────────────────────────────────────────────────────────────────────────


def _sanitize_prompt_name(name: str) -> str:
    """MLflow Prompt Registry allows only [a-zA-Z0-9._-]. Map DB-style 'factors/foo'
    to MLflow-safe 'factors.foo'. Round-tripping not required — we store the
    resulting URI verbatim back on the DB row.
    """
    return name.replace("/", ".")


def register_prompt(
    name: str,
    template: str,
    sha256: str,
    tags: Mapping[str, str] | None = None,
) -> str | None:
    """Register / fetch a prompt version. Idempotent on sha256 via tag search.

    Returns a `prompts:/<name>/<version>` URI string, or None on failure.
    """
    client = _get_client()
    if client is None or _mlflow_module is None:
        return None

    safe_name = _sanitize_prompt_name(name)
    full_tags: dict[str, str] = {"sha256": sha256, "db_name": name}
    if tags:
        full_tags.update({k: str(v) for k, v in tags.items()})

    try:
        existing = _search_existing_prompt(client, safe_name, sha256)
        if existing is not None:
            return existing
    except Exception as exc:
        log.debug("mlflow.search_prompts_skipped", name=safe_name, error=str(exc))

    try:
        register = getattr(_mlflow_module, "register_prompt", None)
        if register is None:
            log.warning(
                "mlflow.register_prompt_unsupported",
                name=safe_name,
                mlflow_version=getattr(_mlflow_module, "__version__", "?"),
            )
            return None
        prompt = register(
            name=safe_name,
            template=template,
            commit_message=f"sha256={sha256[:12]}",
            tags=full_tags,
        )
        return getattr(prompt, "uri", None)
    except Exception as exc:
        log.warning("mlflow.register_prompt_failed", name=safe_name, error=str(exc))
        return None


def _search_existing_prompt(client: Any, name: str, sha256: str) -> str | None:
    """Use whichever Prompt Registry search surface this MLflow version exposes."""
    # Try top-level `mlflow.search_prompts` first (2.18+).
    search = getattr(_mlflow_module, "search_prompts", None)
    candidates: list[Any] = []
    if callable(search):
        candidates = list(search(filter_string=f"name = '{name}'", max_results=100))
    else:
        client_search = getattr(client, "search_prompts", None)
        if callable(client_search):
            candidates = list(client_search(filter_string=f"name = '{name}'", max_results=100))

    for p in candidates:
        tags = getattr(p, "tags", {}) or {}
        if isinstance(tags, list):  # some MLflow versions return list[PromptTag]
            tags = {t.key: t.value for t in tags}
        if tags.get("sha256") == sha256:
            version = getattr(p, "version", None)
            if version is not None:
                return f"prompts:/{name}/{version}"
    return None


# ───────────────────────────────────────────────────────────────────────────
# Runs — parent (pipeline tick) + child (per-factor evaluation)
# ───────────────────────────────────────────────────────────────────────────


@contextmanager
def start_run(
    experiment_id: str | None,
    run_name: str,
    tags: Mapping[str, str] | None = None,
    parent_run_id: str | None = None,
) -> Iterator[str | None]:
    """Yield an MLflow run_id, or None if MLflow is off / unavailable.

    Uses MlflowClient.create_run / set_terminated rather than the thread-local
    `mlflow.start_run()` to stay safe in async code where multiple runs may
    interleave on the same thread.
    """
    client = _get_client()
    if client is None or not experiment_id:
        yield None
        return

    run_tags: dict[str, str] = {"mlflow.runName": run_name}
    if parent_run_id:
        run_tags["mlflow.parentRunId"] = parent_run_id
    if tags:
        run_tags.update({k: str(v) for k, v in tags.items()})

    run_id: str | None = None
    try:
        run = client.create_run(experiment_id=experiment_id, tags=run_tags)
        run_id = run.info.run_id
    except Exception as exc:
        log.warning("mlflow.create_run_failed", run_name=run_name, error=str(exc))
        yield None
        return

    try:
        yield run_id
    except Exception:
        _safe_terminate(client, run_id, "FAILED")
        raise
    else:
        _safe_terminate(client, run_id, "FINISHED")


def _safe_terminate(client: Any, run_id: str, status: str) -> None:
    try:
        client.set_terminated(run_id, status=status)
    except Exception as exc:
        log.warning("mlflow.set_terminated_failed", run_id=run_id, error=str(exc))


def _log_batch(client: Any, run_id: str, *, metrics=(), params=(), tags=()) -> None:
    """One REST call instead of one per key. The per-key helpers below used to
    issue a synchronous HTTP request per param/metric/tag — ~20 calls per
    factor child run, serialized inside the pipeline's persist stage (the
    config comment on PMI_MLFLOW_FACTOR_CHILD_RUNS measured them dominating
    fresh ticks). Chunked to MLflow's per-request caps."""
    for i in range(0, max(len(metrics), len(params), len(tags), 1), 100):
        m, p, t = metrics[i : i + 100], params[i : i + 100], tags[i : i + 100]
        if m or p or t:
            client.log_batch(run_id, metrics=m, params=p, tags=t)


def log_params(run_id: str | None, params: Mapping[str, Any]) -> None:
    client = _get_client()
    if client is None or run_id is None:
        return
    try:
        from mlflow.entities import Param

        # MLflow param values must be strings ≤ 6000 chars.
        entities = [
            Param(key, str(value)[:6000])
            for key, value in params.items()
            if value is not None
        ]
        _log_batch(client, run_id, params=entities)
    except Exception as exc:
        log.warning("mlflow.log_params_failed", run_id=run_id, error=str(exc))


def log_metrics(run_id: str | None, metrics: Mapping[str, float | int | None]) -> None:
    client = _get_client()
    if client is None or run_id is None:
        return
    try:
        import time as _time

        from mlflow.entities import Metric

        now_ms = int(_time.time() * 1000)
        entities = []
        for key, value in metrics.items():
            if value is None:
                continue
            try:
                fval = float(value)
            except (TypeError, ValueError):
                continue
            entities.append(Metric(key, fval, now_ms, 0))
        _log_batch(client, run_id, metrics=entities)
    except Exception as exc:
        log.warning("mlflow.log_metrics_failed", run_id=run_id, error=str(exc))


def log_metric_series(
    run_id: str | None,
    key: str,
    points: Iterable[tuple[float | None, int, int | None]],
) -> None:
    """Log a stepped metric time-series: one point per ``(value, step, ts_ms)``.

    Backtest uses this for the per-replay-point score curve. Logging with an
    explicit ``step`` (the point index) is what makes multiple backtest runs
    overlay on one x-axis in the MLflow compare view — pin two index versions,
    replay both over the same window, and their score curves line up point-for
    -point (SHIP-3.6). ``ts_ms`` (epoch millis of the as-of instant) feeds the
    UI's optional wall-clock axis; ``None`` lets MLflow stamp its own.

    Best-effort like every helper here: a ``None`` value is skipped, a
    non-numeric value is skipped, and any MLflow error degrades to a no-op.
    """
    client = _get_client()
    if client is None or run_id is None:
        return
    try:
        import time as _time

        from mlflow.entities import Metric

        now_ms = int(_time.time() * 1000)
        entities = []
        for value, step, ts_ms in points:
            if value is None:
                continue
            try:
                fval = float(value)
            except (TypeError, ValueError):
                continue
            entities.append(Metric(key, fval, ts_ms if ts_ms is not None else now_ms, step))
        _log_batch(client, run_id, metrics=entities)
    except Exception as exc:
        log.warning("mlflow.log_metric_series_failed", run_id=run_id, key=key, error=str(exc))


def set_tags(run_id: str | None, tags: Mapping[str, str]) -> None:
    client = _get_client()
    if client is None or run_id is None:
        return
    try:
        from mlflow.entities import RunTag

        entities = [RunTag(key, str(value)[:5000]) for key, value in tags.items()]
        _log_batch(client, run_id, tags=entities)
    except Exception as exc:
        log.warning("mlflow.set_tags_failed", run_id=run_id, error=str(exc))


def log_text(run_id: str | None, content: str, artifact_path: str) -> None:
    client = _get_client()
    if client is None or run_id is None:
        return
    try:
        client.log_text(run_id, content, artifact_path)
    except Exception as exc:
        log.warning(
            "mlflow.log_text_failed", run_id=run_id, path=artifact_path, error=str(exc)
        )
