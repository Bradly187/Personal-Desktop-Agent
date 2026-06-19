"""DevAgent emits the live-DAG event spine, correlated by the inbound trace_id.

A chat-originated plan must publish:
  * plan.generated — carrying each step's deps (the DAG edges),
  * dag.step_started / dag.step_completed for every step,
  * step.failed (already existed) now tagged with the trace_id,
all under the SAME trace_id the chat server passed in, so the live UI can match
events to the originating socket.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent
from core.goal_session import GoalSessionStore


class _RR:
    def __init__(self, text: str, ok: bool = True, model: str = "plan-model"):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = None if ok else "err"


class _FakeBus:
    """Records every publish as (topic, payload, trace_id)."""
    def __init__(self):
        self.events = []

    async def publish(self, topic, payload, source, **kw):
        self.events.append((topic, payload, kw.get("trace_id")))
        return 1


class _FakeScheduler:
    """Runs fan-out children inline so the DAG-wave path executes in-process."""
    async def fan_out(self, coros, label="", **kw):
        return [await c for c in coros]


_PLAN_JSON = json.dumps({"steps": [
    {"action": "READ_FILE", "args": "a.txt"},
    {"action": "READ_FILE", "args": "b.txt"},
    {"action": "EXPLAIN", "body": "combine a and b", "after": [1, 2]},
]})


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


def _build_agent(plan_text: str, run_step) -> tuple[DevAgent, _FakeBus]:
    router = AsyncMock()
    router.infer = AsyncMock(return_value=_RR(plan_text))
    agent = DevAgent(router=router)
    bus = _FakeBus()
    agent.set_event_bus(bus)
    agent.set_scheduler(_FakeScheduler())
    # Approve immediately; stub the side-channels the locked plan loop touches.
    agent._approve_plan_upfront = AsyncMock(return_value="approved")
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="summary")
    agent._persist_run = AsyncMock()
    agent._speak_plan_completion = AsyncMock()
    agent._start_run = AsyncMock(return_value=1)   # run_id >= 0 → step.failed publishes
    agent._db = lambda: None                       # no DB in test
    agent._run_step_with_retry = run_step
    return agent, bus


async def test_plan_generated_and_step_events_carry_trace_id():
    async def run_step(step):
        step.success = True
        step.latency_ms = 1.0
        step.result = "ok"
        return True

    agent, bus = _build_agent(_PLAN_JSON, run_step)
    result = await agent.plan_and_run("combine the files", trace_id="TID-123")
    assert result.success is True

    topics = [t for (t, _p, _tid) in bus.events]
    assert "plan.generated" in topics

    # plan.generated carries the deps (DAG edges): step 3 depends on 1 and 2.
    plan_ev = next(p for (t, p, _tid) in bus.events if t == "plan.generated")
    steps = {s["n"]: s for s in plan_ev["steps"]}
    assert steps[3]["deps"] == [1, 2]
    assert steps[1]["deps"] == []

    # Every step emits started + completed, all under the inbound trace_id.
    started = {p["n"] for (t, p, _tid) in bus.events if t == "dag.step_started"}
    completed = {p["n"] for (t, p, _tid) in bus.events if t == "dag.step_completed"}
    assert started == {1, 2, 3}
    assert completed == {1, 2, 3}
    assert all(tid == "TID-123" for (_t, _p, tid) in bus.events)


async def test_step_failed_event_carries_trace_id():
    async def run_step(step):
        if step.args == "b.txt":
            step.success = False
            step.latency_ms = 1.0
            step.result = "boom"
            return False
        step.success = True
        step.latency_ms = 1.0
        step.result = "ok"
        return True

    agent, bus = _build_agent(_PLAN_JSON, run_step)
    # Halt instead of replanning, so the test is bounded.
    agent._try_replan = AsyncMock(return_value=None)
    agent._halt_and_compensate = AsyncMock()

    await agent.plan_and_run("combine the files", trace_id="TID-XYZ")

    failed = [(p, tid) for (t, p, tid) in bus.events if t == "step.failed"]
    assert failed, "expected a step.failed event"
    payload, tid = failed[0]
    assert tid == "TID-XYZ"
    assert payload["action"] == "READ_FILE"
