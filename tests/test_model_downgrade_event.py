"""ModelRouter downgrade observability — a VRAM fallback pick is surfaced as a
WARNING + one model.downgraded event on state change (not per request), and
recovery to the primary clears the state silently.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.model_router import ModelRouter


def _router_with_free_gb(monkeypatch, free_gb: float) -> ModelRouter:
    r = ModelRouter()
    monkeypatch.setattr("inference.model_router._free_vram_gb", lambda: free_gb)
    monkeypatch.setattr(r, "_apply_override", lambda domain, free: None)
    return r


async def test_downgrade_publishes_once_per_state(monkeypatch, caplog):
    r = _router_with_free_gb(monkeypatch, 6.0)     # too tight for a 30B coder
    bus = MagicMock()
    bus.publish = AsyncMock()
    r.set_event_bus(bus)

    p1 = r.select_profile("code")
    assert p1.name != "qwen3-coder:30b"            # downgraded
    p2 = r.select_profile("code")                  # same tight VRAM again
    assert p2.name == p1.name

    import asyncio
    await asyncio.sleep(0)                          # let fire_and_log task run
    assert bus.publish.await_count == 1             # state change → exactly one
    topic, payload = bus.publish.await_args.args[0], bus.publish.await_args.args[1]
    assert topic == "model.downgraded"
    assert payload["domain"] == "code"
    assert payload["wanted"] == "qwen3-coder:30b"
    assert payload["got"] == p1.name


async def test_recovery_clears_state_and_republishes_next_downgrade(monkeypatch):
    r = _router_with_free_gb(monkeypatch, 6.0)
    bus = MagicMock()
    bus.publish = AsyncMock()
    r.set_event_bus(bus)

    import asyncio
    r.select_profile("code")                        # downgrade #1
    monkeypatch.setattr("inference.model_router._free_vram_gb", lambda: 30.0)
    assert r.select_profile("code").name == "qwen3-coder:30b"   # recovered
    monkeypatch.setattr("inference.model_router._free_vram_gb", lambda: 6.0)
    r.select_profile("code")                        # downgrade #2 → new event
    await asyncio.sleep(0)
    assert bus.publish.await_count == 2


def test_primary_pick_emits_nothing(monkeypatch):
    r = _router_with_free_gb(monkeypatch, 30.0)     # plenty of VRAM
    bus = MagicMock()
    bus.publish = AsyncMock()
    r.set_event_bus(bus)
    assert r.select_profile("code").name == "qwen3-coder:30b"
    assert bus.publish.await_count == 0


def test_no_bus_means_warning_only(monkeypatch, caplog):
    r = _router_with_free_gb(monkeypatch, 6.0)      # no set_event_bus
    with caplog.at_level("WARNING"):
        r.select_profile("code")
    assert any("DOWNGRADED" in rec.message for rec in caplog.records)
