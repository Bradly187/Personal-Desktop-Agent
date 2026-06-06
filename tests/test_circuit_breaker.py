"""Tests for CircuitBreaker (orchestration gap #4) and its OllamaInference wiring.

Deterministic via an injected clock — no real time, no real Ollama.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.circuit_breaker import CircuitBreaker


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _cb(clock, fail_threshold=3, cooldown_s=30.0):
    return CircuitBreaker(name="t", fail_threshold=fail_threshold,
                          cooldown_s=cooldown_s, time_fn=clock)


def test_closed_allows_by_default():
    cb = _cb(_Clock())
    assert cb.state == "closed"
    assert cb.allow() is True


def test_opens_after_threshold_failures():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=3)
    for _ in range(2):
        cb.record_failure()
    assert cb.state == "closed"        # not yet
    assert cb.allow() is True
    cb.record_failure()                # 3rd
    assert cb.state == "open"
    assert cb.allow() is False         # fast-fail during cooldown


def test_half_open_after_cooldown_then_close_on_success():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, cooldown_s=30.0)
    cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False

    clock.advance(31.0)
    assert cb.allow() is True           # cooldown elapsed → half-open probe
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow() is True


def test_half_open_probe_failure_reopens():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, cooldown_s=10.0)
    cb.record_failure()                 # open
    clock.advance(11.0)
    assert cb.allow() is True           # half-open probe admitted
    cb.record_failure()                 # probe fails
    assert cb.state == "open"
    assert cb.allow() is False          # cooldown restarted


def test_half_open_admits_only_one_probe():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, cooldown_s=10.0)
    cb.record_failure()
    clock.advance(11.0)
    assert cb.allow() is True           # first probe
    assert cb.allow() is False          # concurrent caller rejected until outcome


def test_success_resets_failure_count():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()                 # resets
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"         # 2 < 3 after reset


# ---------------------------------------------------------------------------
# OllamaInference wiring
# ---------------------------------------------------------------------------

from core.command_executor import Command
from inference.local_inference import OllamaInference


async def test_ollama_open_breaker_fast_fails(monkeypatch):
    """When the breaker is open, infer() returns immediately without a network call."""
    b = OllamaInference(use_tools=True)
    # Force the breaker open.
    for _ in range(3):
        b._breaker.record_failure()
    assert b._breaker.state == "open"

    called = {"n": 0}

    async def _chat_should_not_run(*a, **k):
        called["n"] += 1
        return {"message": {}}

    b._chat = _chat_should_not_run
    out = await b.infer(Command(text="click x", action="", source="voice"))
    assert "circuit open" in out
    assert called["n"] == 0              # no network attempt while open


async def test_ollama_success_keeps_breaker_closed():
    b = OllamaInference(use_tools=True)

    async def _chat_ok(messages, tools=None):
        return {"message": {"tool_calls": [
            {"function": {"name": "desktop_action",
                          "arguments": {"verb": "CLICK", "argument": "x"}}}]}}

    b._chat = _chat_ok
    out = await b.infer(Command(text="click x", action="", source="voice"))
    assert out == "CLICK x"
    assert b._breaker.state == "closed"
