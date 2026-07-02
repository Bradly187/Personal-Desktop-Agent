"""Signature tripwire for the most-stubbed seams.

Dozens of tests replace these callables with hand-written stubs or AsyncMocks
(`monkeypatch.setattr(agent, "plan_and_run", ...)`, `agent._plan_and_run_locked
= ...`). A stub keeps "passing" after the real signature changes — the break
surfaces later, in a different test, as a confusing TypeError (this happened
twice: PR #151 and the 2026-07-01 `_locked_body` arity fix, both drift from
PR #149's `extra_context` plumbing).

This module turns that silent drift into ONE loud, self-explanatory failure:
when you change a pinned signature, update the pin here AND grep the test tree
for the attribute name to fix every stub in the same change.
"""
from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_HOWTO = ("signature changed — update this pin AND every test stub: "
          "grep tests/ for the attribute name to find them")


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_dev_agent_plan_and_run_signature():
    from inference.dev_agent import DevAgent
    assert _params(DevAgent.plan_and_run) == [
        "self", "goal", "trace_id", "seed_context", "extra_context"], _HOWTO
    assert _params(DevAgent._plan_and_run_locked) == [
        "self", "goal", "cmd_trace_id", "seed_context", "turn_context"], _HOWTO


def test_dev_agent_handle_signature():
    from inference.dev_agent import DevAgent
    assert _params(DevAgent.handle) == [
        "self", "text", "screenshot_b64", "trace_id", "attachment_context"], _HOWTO


def test_model_router_infer_signature():
    from inference.model_router import ModelRouter
    assert _params(ModelRouter.infer) == [
        "self", "domain", "user_text", "screenshot_b64", "context"], _HOWTO


def test_coordinator_route_and_executor_execute_signatures():
    from core.command_executor import CommandExecutor
    from core.hybrid_coordinator import HybridCoordinator
    assert _params(HybridCoordinator.route) == ["self", "cmd"], _HOWTO
    assert _params(CommandExecutor.execute) == ["self", "cmd"], _HOWTO


def test_command_dto_fields():
    """Command is the universal pipeline DTO — every test that builds one by
    keyword relies on this exact field set."""
    from core.command_executor import Command
    assert [f.name for f in dataclasses.fields(Command)] == [
        "text", "action", "source", "whisper_logprob", "gesture_confidence",
        "session_context", "gaze_coords", "params", "trace_id"], _HOWTO
