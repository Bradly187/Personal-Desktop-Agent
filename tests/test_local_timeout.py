"""Tests for the local-inference circuit-breaker in InferenceRunner.run_local.

A hung local backend (Ollama wedged, GPU stuck mid-flare, stalled model reload)
must not stall the accessibility pipeline indefinitely. run_local wraps the
inference call in an asyncio.timeout and degrades to CLARIFY on expiry — mirroring
the existing cloud-path guard in run_cloud.

Covers:
- A hung local infer() trips the timeout and returns the CLARIFY fallback
- A fast local infer() returns its action normally (no false trip)
- local_timeout_s is configurable via CoordinatorConfig
- A timeout in run_local does not raise out of route() — the pipeline survives
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import CoordinatorConfig, HybridCoordinator


def _cmd(text: str = "scroll down", source: str = "touch") -> Command:
    # source="touch" → bypass path → run_local is called directly with no gates.
    return Command(text=text, action="", source=source, whisper_logprob=0.0)


def _coord(timeout_s: float) -> HybridCoordinator:
    cfg = CoordinatorConfig(local_timeout_s=timeout_s)
    return HybridCoordinator(config=cfg)


async def test_local_timeout_returns_clarify():
    """A local infer that hangs past local_timeout_s yields the CLARIFY fallback."""
    coord = _coord(timeout_s=0.05)

    async def _hang(cmd, few_shot_examples=None, counterexamples=None):
        await asyncio.sleep(5.0)
        return "SCROLL down"

    coord._local.infer = _hang

    result = await coord._inference.run_local(_cmd())
    assert result == "CLARIFY local inference timed out"


async def test_local_fast_path_no_false_trip():
    """A fast local infer returns its action normally without tripping the timeout."""
    coord = _coord(timeout_s=5.0)
    coord._local.infer = AsyncMock(return_value="SCROLL down")

    result = await coord._inference.run_local(_cmd())
    assert result == "SCROLL down"
    coord._local.infer.assert_awaited_once()


async def test_local_timeout_is_configurable():
    """CoordinatorConfig.local_timeout_s drives the circuit-breaker window."""
    cfg = CoordinatorConfig(local_timeout_s=3.5)
    assert cfg.local_timeout_s == 3.5
    # Default is a sane non-zero ceiling.
    assert CoordinatorConfig().local_timeout_s > 0


async def test_route_survives_local_hang():
    """A hung local backend degrades route() to a CLARIFY execution, not a crash."""
    coord = _coord(timeout_s=0.05)

    async def _hang(cmd, few_shot_examples=None, counterexamples=None):
        await asyncio.sleep(5.0)
        return "SCROLL down"

    coord._local.infer = _hang
    executed = {}

    async def _exec(action_str, cmd, route_label="local"):
        executed["action"] = action_str
        return {"status": "ok"}

    coord._action_executor.execute_action = _exec

    result = await coord.route(_cmd())
    # route() returns normally and the CLARIFY fallback reached the executor.
    assert result["status"] == "ok"
    assert executed["action"] == "CLARIFY local inference timed out"
