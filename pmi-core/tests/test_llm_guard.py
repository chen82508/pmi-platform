"""`_LLMGuard`: what does and does not open the per-tick circuit breaker."""

from __future__ import annotations

from pmi_core.config import settings
from pmi_core.engine.pipeline import _LLMGuard
from pmi_core.llm.base import ParseError


def test_availability_failures_open_the_circuit() -> None:
    guard = _LLMGuard()
    for _ in range(settings.llm_circuit_breaker_failures):
        guard.record_failure(ConnectionError("provider unreachable"))
    assert guard.check() == "circuit_open"


def test_a_success_resets_the_streak() -> None:
    guard = _LLMGuard()
    for _ in range(settings.llm_circuit_breaker_failures - 1):
        guard.record_failure(ConnectionError("blip"))
    guard.record_success(0.0)
    guard.record_failure(ConnectionError("blip"))
    assert guard.check() is None


def test_parse_errors_never_open_the_circuit() -> None:
    """A small local model emitting `{}` is a per-market miss, not an outage.

    Counting it would blackout the rest of the tick, and stub rows aren't
    cache-reusable, so the next tick would re-pay every skipped call.
    """
    guard = _LLMGuard()
    for _ in range(settings.llm_circuit_breaker_failures * 3):
        guard.record_failure(ParseError("missing 'value' key in {}"))
    assert guard.check() is None
    assert guard.consecutive_failures == 0


def test_parse_errors_do_not_mask_a_real_outage() -> None:
    """Interleaved parse errors must not reset an availability streak."""
    guard = _LLMGuard()
    for _ in range(settings.llm_circuit_breaker_failures):
        guard.record_failure(ConnectionError("provider unreachable"))
        guard.record_failure(ParseError("missing 'value' key in {}"))
    assert guard.check() == "circuit_open"


def test_budget_cap_still_opens_the_circuit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_budget_usd_per_tick", 0.05)
    guard = _LLMGuard()
    guard.record_success(0.04)
    assert guard.check() is None
    guard.record_success(0.02)
    assert guard.check() == "budget_exceeded"
