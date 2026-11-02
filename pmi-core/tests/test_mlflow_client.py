"""Unit tests for pmi_core.mlflow_client — focus is the graceful-degradation
contract, not the MLflow protocol itself.

Coverage:
    - Hard-disabled via settings.mlflow_enabled = False
    - Server unreachable (MlflowClient ctor raises)
    - Registry API absent (older mlflow without register_prompt)
    - Idempotency: existing prompt with same sha256 is reused
    - Run lifecycle: create_run + log + set_terminated FINISHED
    - Run lifecycle: exception → set_terminated FAILED, exception re-raised
    - log_params / log_metrics / set_tags no-op when run_id=None
    - log_metrics drops None and non-numeric values
    - ensure_experiment hits get-then-create only on miss
    - _sanitize_prompt_name converts slashes to dots
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pmi_core import mlflow_client

# ─────────────────────────────────────────────────────────────────────────
# Disabled + unreachable
# ─────────────────────────────────────────────────────────────────────────


def test_disabled_via_settings_returns_none_for_all(monkeypatch: pytest.MonkeyPatch) -> None:
    from pmi_core import config

    monkeypatch.setattr(config.settings, "mlflow_enabled", False)

    assert mlflow_client.is_enabled() is False
    assert mlflow_client.ensure_experiment("any") is None
    assert mlflow_client.register_prompt("a", "t", "sha") is None
    with mlflow_client.start_run("1", "name") as run_id:
        assert run_id is None
    # No-ops shouldn't raise even with None run_id.
    mlflow_client.log_params(None, {"k": "v"})
    mlflow_client.log_metrics(None, {"m": 1.0})
    mlflow_client.set_tags(None, {"t": "v"})
    mlflow_client.log_text(None, "hi", "p.txt")


def test_unreachable_soft_disables_after_first_failure(
    fake_mlflow_unreachable: None,
) -> None:
    """Once MlflowClient() raises, subsequent calls short-circuit without retry."""
    assert mlflow_client.is_enabled() is False
    # Second call should also be False — and importantly must not re-attempt
    # construction (we can't easily assert that, but the soft-disable flag is
    # the contract; check the public surface stays None.)
    assert mlflow_client.ensure_experiment("x") is None
    assert mlflow_client.register_prompt("a", "t", "sha") is None
    with mlflow_client.start_run("1", "name") as run_id:
        assert run_id is None


# ─────────────────────────────────────────────────────────────────────────
# Experiments
# ─────────────────────────────────────────────────────────────────────────


def test_ensure_experiment_creates_when_missing(fake_mlflow: Any) -> None:
    exp_id = mlflow_client.ensure_experiment("polymarket-war-index")
    # Prefix applied
    assert "pmi.polymarket-war-index" in fake_mlflow.experiments_by_name
    assert exp_id == fake_mlflow.experiments_by_name["pmi.polymarket-war-index"]
    # On second call should NOT create again
    again = mlflow_client.ensure_experiment("polymarket-war-index")
    assert again == exp_id
    create_calls = [c for c in fake_mlflow.calls if c[0] == "create_experiment"]
    assert len(create_calls) == 1


# ─────────────────────────────────────────────────────────────────────────
# Run lifecycle
# ─────────────────────────────────────────────────────────────────────────


def test_start_run_emits_create_then_finished(fake_mlflow: Any) -> None:
    with mlflow_client.start_run("exp-1", "tick:foo", tags={"k": "v"}) as run_id:
        assert run_id == "run-1"
        # Calls inside body
        mlflow_client.log_params(run_id, {"a": 1, "b": "x"})
        mlflow_client.log_metrics(run_id, {"score": 50.0})
        mlflow_client.set_tags(run_id, {"manual": "tag"})

    assert fake_mlflow.terminated["run-1"] == "FINISHED"
    # Params recorded (stringified)
    assert fake_mlflow.params["run-1"] == [("a", "1"), ("b", "x")]
    assert fake_mlflow.metrics["run-1"] == [("score", 50.0)]
    assert fake_mlflow.tags["run-1"] == [("manual", "tag")]


def test_start_run_emits_failed_on_exception(fake_mlflow: Any) -> None:
    with pytest.raises(RuntimeError):
        with mlflow_client.start_run("exp-1", "tick:foo") as run_id:
            assert run_id == "run-1"
            raise RuntimeError("boom")
    assert fake_mlflow.terminated["run-1"] == "FAILED"


def test_start_run_with_no_experiment_yields_none(fake_mlflow: Any) -> None:
    """experiment_id=None short-circuits without contacting MLflow."""
    with mlflow_client.start_run(None, "tick:foo") as run_id:
        assert run_id is None
    create_calls = [c for c in fake_mlflow.calls if c[0] == "create_run"]
    assert create_calls == []


def test_parent_run_tag_set_on_child(fake_mlflow: Any) -> None:
    with mlflow_client.start_run("exp-1", "child", parent_run_id="parent-x") as run_id:
        assert run_id == "run-1"
    create_args = next(c for c in fake_mlflow.calls if c[0] == "create_run")
    _, args, _ = create_args
    tags = args[1]
    assert tags["mlflow.parentRunId"] == "parent-x"
    assert tags["mlflow.runName"] == "child"


# ─────────────────────────────────────────────────────────────────────────
# log_* edge cases
# ─────────────────────────────────────────────────────────────────────────


def test_log_metrics_skips_none_and_non_numeric(fake_mlflow: Any) -> None:
    with mlflow_client.start_run("exp-1", "n") as run_id:
        mlflow_client.log_metrics(
            run_id,
            {"good": 1.5, "none": None, "nan_str": "not a number", "int_ok": 3},
        )
    metrics = dict(fake_mlflow.metrics["run-1"])
    assert metrics == {"good": 1.5, "int_ok": 3.0}


def test_log_params_skips_none(fake_mlflow: Any) -> None:
    with mlflow_client.start_run("exp-1", "n") as run_id:
        mlflow_client.log_params(run_id, {"a": "ok", "b": None, "c": 0})
    keys = [k for k, _ in fake_mlflow.params["run-1"]]
    assert keys == ["a", "c"]


def test_log_helpers_noop_when_run_id_none(fake_mlflow: Any) -> None:
    mlflow_client.log_params(None, {"a": 1})
    mlflow_client.log_metrics(None, {"a": 1})
    mlflow_client.set_tags(None, {"a": "b"})
    mlflow_client.log_text(None, "hello", "p.txt")
    # None of these should have caused any client interaction
    interesting = [c for c in fake_mlflow.calls if c[0] in ("log_param", "log_metric", "set_tag", "log_text")]
    assert interesting == []


# ─────────────────────────────────────────────────────────────────────────
# Prompt registry
# ─────────────────────────────────────────────────────────────────────────


def test_register_prompt_returns_uri(
    monkeypatch: pytest.MonkeyPatch, make_mlflow_module, fake_mlflow: Any
) -> None:
    """Happy path: top-level mlflow.register_prompt returns a Prompt-like object."""

    calls: list[dict] = []

    def fake_register_prompt(**kwargs):
        calls.append(kwargs)
        from tests.conftest import FakePrompt

        return FakePrompt(
            uri="prompts:/factors.foo/1",
            version=1,
            tags=kwargs.get("tags", {}),
        )

    mlflow_mod, tracking_mod = make_mlflow_module(
        fake_mlflow, register_prompt_fn=fake_register_prompt
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)

    uri = mlflow_client.register_prompt(
        name="factors/foo",
        template="hello {x}",
        sha256="abc123",
        tags={"factor": "foo"},
    )
    assert uri == "prompts:/factors.foo/1"
    assert len(calls) == 1
    # Name was sanitized (slash → dot)
    assert calls[0]["name"] == "factors.foo"
    # sha256 + db_name carried in tags
    assert calls[0]["tags"]["sha256"] == "abc123"
    assert calls[0]["tags"]["db_name"] == "factors/foo"
    assert calls[0]["tags"]["factor"] == "foo"


def test_register_prompt_idempotent_on_existing_sha(
    monkeypatch: pytest.MonkeyPatch, make_mlflow_module, fake_mlflow: Any
) -> None:
    """If a prompt version with the same sha256 already exists, return its URI."""
    from tests.conftest import FakePrompt

    register_calls: list[dict] = []

    def fake_register(**kwargs):
        register_calls.append(kwargs)
        return FakePrompt(uri="prompts:/factors.foo/2", version=2, tags={})

    def fake_search(filter_string: str, max_results: int):
        # Return one existing matching prompt version
        return [FakePrompt(uri="prompts:/factors.foo/1", version=1, tags={"sha256": "abc123"})]

    mlflow_mod, tracking_mod = make_mlflow_module(
        fake_mlflow, register_prompt_fn=fake_register, search_prompts_fn=fake_search
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)

    uri = mlflow_client.register_prompt(name="factors/foo", template="t", sha256="abc123")
    assert uri == "prompts:/factors.foo/1"
    assert register_calls == []  # never reached


def test_register_prompt_returns_none_when_api_missing(
    monkeypatch: pytest.MonkeyPatch, make_mlflow_module, fake_mlflow: Any
) -> None:
    """Older mlflow without register_prompt — wrapper returns None gracefully."""
    mlflow_mod, tracking_mod = make_mlflow_module(fake_mlflow)  # no register_prompt_fn

    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)

    uri = mlflow_client.register_prompt(name="factors/foo", template="t", sha256="abc")
    assert uri is None


def test_register_prompt_swallows_server_exceptions(
    monkeypatch: pytest.MonkeyPatch, make_mlflow_module, fake_mlflow: Any
) -> None:
    """Server returns 4xx/5xx — wrapper returns None, doesn't propagate."""

    def fake_register(**_):
        raise RuntimeError("INVALID_PARAMETER_VALUE: dummy-source rejected")

    mlflow_mod, tracking_mod = make_mlflow_module(
        fake_mlflow, register_prompt_fn=fake_register
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_mod)

    assert mlflow_client.register_prompt("factors/foo", "t", "sha") is None


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def test_sanitize_prompt_name() -> None:
    from pmi_core.mlflow_client import _sanitize_prompt_name

    assert _sanitize_prompt_name("factors/armed_conflict") == "factors.armed_conflict"
    assert _sanitize_prompt_name("a/b/c") == "a.b.c"
    assert _sanitize_prompt_name("already.dotted") == "already.dotted"


def test_is_enabled_true_when_fake_module_loaded(fake_mlflow: Any) -> None:
    assert mlflow_client.is_enabled() is True
