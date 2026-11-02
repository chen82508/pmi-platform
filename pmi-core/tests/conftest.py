"""Shared pytest fixtures for pmi-core unit tests."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

# ──────────────────────────────────────────────────────────────────────────
# mlflow_client fixtures — reset module-level state between tests
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_mlflow_client_state() -> Iterator[None]:
    """Force the lazy-cached MlflowClient to re-init between every test."""
    from pmi_core import mlflow_client

    mlflow_client.reset_for_tests()
    yield
    mlflow_client.reset_for_tests()


# ──────────────────────────────────────────────────────────────────────────
# Recording fake MlflowClient — covers the success path
# ──────────────────────────────────────────────────────────────────────────


class FakeRunInfo:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


class FakeRun:
    def __init__(self, run_id: str) -> None:
        self.info = FakeRunInfo(run_id)


class FakeExperiment:
    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id


class FakePrompt:
    def __init__(self, uri: str, version: int, tags: dict[str, str]) -> None:
        self.uri = uri
        self.version = version
        self.tags = tags


class FakeMlflowClient:
    """Records every call so tests can assert on the sequence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.experiments_by_name: dict[str, str] = {}
        self.next_run_seq = 0
        self.terminated: dict[str, str] = {}
        self.params: dict[str, list[tuple[str, str]]] = {}
        self.metrics: dict[str, list[tuple[str, float]]] = {}
        self.tags: dict[str, list[tuple[str, str]]] = {}
        self.artifacts: dict[str, list[tuple[str, str]]] = {}

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def get_experiment_by_name(self, name: str) -> FakeExperiment | None:
        self._record("get_experiment_by_name", name)
        exp_id = self.experiments_by_name.get(name)
        return FakeExperiment(exp_id) if exp_id else None

    def create_experiment(self, name: str) -> str:
        self._record("create_experiment", name)
        exp_id = str(len(self.experiments_by_name) + 1)
        self.experiments_by_name[name] = exp_id
        return exp_id

    def create_run(self, experiment_id: str, tags: dict[str, str]) -> FakeRun:
        self._record("create_run", experiment_id, tags)
        self.next_run_seq += 1
        return FakeRun(f"run-{self.next_run_seq}")

    def set_terminated(self, run_id: str, status: str) -> None:
        self._record("set_terminated", run_id, status)
        self.terminated[run_id] = status

    def log_param(self, run_id: str, key: str, value: str) -> None:
        self._record("log_param", run_id, key, value)
        self.params.setdefault(run_id, []).append((key, value))

    def log_metric(self, run_id: str, key: str, value: float) -> None:
        self._record("log_metric", run_id, key, value)
        self.metrics.setdefault(run_id, []).append((key, value))

    def log_batch(self, run_id: str, metrics=(), params=(), tags=()) -> None:
        self._record("log_batch", run_id, metrics, params, tags)
        for p in params:
            self.params.setdefault(run_id, []).append((p.key, p.value))
        for m in metrics:
            self.metrics.setdefault(run_id, []).append((m.key, m.value))
        for t in tags:
            self.tags.setdefault(run_id, []).append((t.key, t.value))

    def set_tag(self, run_id: str, key: str, value: str) -> None:
        self._record("set_tag", run_id, key, value)
        self.tags.setdefault(run_id, []).append((key, value))

    def log_text(self, run_id: str, text: str, artifact_path: str) -> None:
        self._record("log_text", run_id, text, artifact_path)
        self.artifacts.setdefault(run_id, []).append((artifact_path, text))


# ──────────────────────────────────────────────────────────────────────────
# Fake `mlflow` module installed into sys.modules
# ──────────────────────────────────────────────────────────────────────────


class _ExceptionsModule(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("mlflow.exceptions")
        self.MlflowException = type("MlflowException", (Exception,), {})


def _make_mlflow_module(
    client: FakeMlflowClient,
    *,
    register_prompt_fn=None,
    search_prompts_fn=None,
    raise_on_set_tracking_uri: Exception | None = None,
    raise_on_import: Exception | None = None,
) -> tuple[types.ModuleType, types.ModuleType]:
    """Build a fake `mlflow` + `mlflow.tracking` module pair.

    When raise_on_import is set, MlflowClient() raises — simulating an
    unreachable tracking server at construction time.
    """
    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.__version__ = "2.22.5"
    mlflow_mod.exceptions = _ExceptionsModule()

    def set_tracking_uri(uri: str) -> None:
        if raise_on_set_tracking_uri is not None:
            raise raise_on_set_tracking_uri
        client._record("set_tracking_uri", uri)

    mlflow_mod.set_tracking_uri = set_tracking_uri

    if register_prompt_fn is not None:
        mlflow_mod.register_prompt = register_prompt_fn
    if search_prompts_fn is not None:
        mlflow_mod.search_prompts = search_prompts_fn

    tracking_mod = types.ModuleType("mlflow.tracking")

    def _client_factory(tracking_uri: str | None = None) -> FakeMlflowClient:
        if raise_on_import is not None:
            raise raise_on_import
        client._record("__init__", tracking_uri)
        return client

    tracking_mod.MlflowClient = _client_factory
    mlflow_mod.tracking = tracking_mod

    # mlflow.entities — the batch-logging value classes (`log_batch` path).
    entities_mod = types.ModuleType("mlflow.entities")

    class _Param:
        def __init__(self, key: str, value: str) -> None:
            self.key, self.value = key, value

    class _Metric:
        def __init__(self, key: str, value: float, timestamp: int, step: int) -> None:
            self.key, self.value, self.timestamp, self.step = key, value, timestamp, step

    class _RunTag:
        def __init__(self, key: str, value: str) -> None:
            self.key, self.value = key, value

    entities_mod.Param = _Param
    entities_mod.Metric = _Metric
    entities_mod.RunTag = _RunTag
    mlflow_mod.entities = entities_mod
    return mlflow_mod, tracking_mod


@pytest.fixture
def fake_mlflow(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMlflowClient]:
    """Install a fake `mlflow` module into sys.modules + force enabled config."""
    client = FakeMlflowClient()
    mlflow_mod, tracking_mod = _make_mlflow_module(client)

    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)
    monkeypatch.setitem(sys.modules, "mlflow.exceptions", mlflow_mod.exceptions)
    monkeypatch.setitem(sys.modules, "mlflow.entities", mlflow_mod.entities)

    from pmi_core import config

    monkeypatch.setattr(config.settings, "mlflow_enabled", True)
    monkeypatch.setattr(config.settings, "mlflow_tracking_uri", "http://test:5000")
    monkeypatch.setattr(config.settings, "mlflow_experiment_prefix", "pmi.")

    yield client


@pytest.fixture
def fake_mlflow_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate an unreachable MLflow server — MlflowClient() raises."""
    client = FakeMlflowClient()
    mlflow_mod, tracking_mod = _make_mlflow_module(
        client, raise_on_import=ConnectionError("server down")
    )

    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)
    monkeypatch.setitem(sys.modules, "mlflow.entities", mlflow_mod.entities)

    from pmi_core import config

    monkeypatch.setattr(config.settings, "mlflow_enabled", True)
    monkeypatch.setattr(config.settings, "mlflow_tracking_uri", "http://unreachable")


@pytest.fixture
def make_mlflow_module():
    """Expose the builder so tests can customise register_prompt / search_prompts."""
    return _make_mlflow_module
