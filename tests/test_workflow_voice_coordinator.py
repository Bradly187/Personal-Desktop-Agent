"""Integration tests for the workflow voice trigger glue in HybridCoordinator.

These exercise ``_maybe_handle_workflow`` end-to-end (decompose → fan_out →
synthesize → speak) with a fake ModelRouter and the *real* WorkflowRunner, so
the orchestration wiring — the novel part this feature adds — is covered. The
coordinator is built via ``__new__`` to skip its heavy constructor; only the
attributes the method touches are set.
"""

import types

import pytest

from core.hybrid_coordinator import HybridCoordinator
from inference.workflow import WorkflowRunner

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, text="", ok=True):
        self.text = text
        self.ok = ok


class _FakeRouter:
    """Routes by prompt content: decomposition, synthesis, or a sub-angle."""

    def __init__(self, *, decompose_ok=True, angle_ok=True):
        self.calls = []
        self._decompose_ok = decompose_ok
        self._angle_ok = angle_ok

    async def infer(self, domain, user_text, context):
        self.calls.append((domain, user_text))
        if "Break the following goal" in user_text:
            if not self._decompose_ok:
                return _Result("", ok=False)
            return _Result("first angle\nsecond angle\nthird angle")
        if "Synthesize the findings" in user_text:
            return _Result("This is the synthesized spoken answer.")
        # a sub-angle inference
        if not self._angle_ok:
            return _Result("", ok=False)
        return _Result(f"sub-answer for {user_text}")


class _FakeDevAgent:
    def __init__(self, router):
        self._router = router


def _coordinator(runner, router, *, twin=None):
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._workflow_runner = runner
    c._dev_agent = _FakeDevAgent(router) if router is not None else None
    c._wf_cfg = {"enabled": True}
    c._twin = twin
    c._whisper = None
    c._spoken = []

    async def _record(text):
        c._spoken.append(text)

    c._tts_speak = _record   # shadow the real (Polly-importing) TTS
    return c


def _cmd(text):
    return types.SimpleNamespace(text=text, source="voice", trace_id="t1")


async def test_happy_path_decomposes_fans_out_and_synthesizes():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    c = _coordinator(runner, router)

    result = await c._maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert result["spoken"] == "This is the synthesized spoken answer."
    assert result["subtasks"] == 3
    assert c._spoken == ["This is the synthesized spoken answer."]
    # 1 decompose + 3 angles + 1 synthesis = 5 inference calls.
    assert len(router.calls) == 5


async def test_not_a_trigger_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    c = _coordinator(runner, router)
    assert await c._maybe_handle_workflow(_cmd("open chrome")) is None
    assert router.calls == []


async def test_disabled_runner_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=False)
    c = _coordinator(runner, router)
    assert await c._maybe_handle_workflow(_cmd("research transformers")) is None
    assert router.calls == []


async def test_flare_falls_through_to_ordinary_routing():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)

    class _Snap:
        pain_day_active = True

    class _Twin:
        async def get_snapshot(self):
            return _Snap()

    c = _coordinator(runner, router, twin=_Twin())
    # A flare returns None (fall through) and issues no inference.
    assert await c._maybe_handle_workflow(_cmd("research transformers")) is None
    assert router.calls == []


async def test_decompose_failure_degrades_to_single_angle():
    router = _FakeRouter(decompose_ok=False)
    runner = WorkflowRunner(router=router, enabled=True)
    c = _coordinator(runner, router)

    result = await c._maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["subtasks"] == 1   # single-angle fallback on the raw goal
    assert result["spoken"] == "This is the synthesized spoken answer."


async def test_all_subagents_failing_speaks_apology():
    router = _FakeRouter(angle_ok=False)
    runner = WorkflowRunner(router=router, enabled=True)
    c = _coordinator(runner, router)

    result = await c._maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert "couldn't" in result["spoken"].lower()
    assert c._spoken and "couldn't" in c._spoken[0].lower()


async def test_missing_router_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    c = _coordinator(runner, router=None)   # no dev agent / router wired
    c._workflow_runner = runner
    assert await c._maybe_handle_workflow(_cmd("research transformers")) is None
