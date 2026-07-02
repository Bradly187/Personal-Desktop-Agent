"""Integration tests for the workflow voice trigger glue in WorkflowHandler.

These exercise ``maybe_handle_workflow`` end-to-end (decompose → fan_out →
synthesize → speak) with a fake ModelRouter and the *real* WorkflowRunner, so
the orchestration wiring — the novel part this feature adds — is covered.
"""

import types

import pytest

from core.workflow_handler import WorkflowHandler
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


def _handler(runner, router, *, twin=None):
    spoken: list = []

    async def _speak_and_suppress(text):
        spoken.append(text)

    wh = WorkflowHandler(
        workflow_runner=lambda: runner,
        dev_agent=lambda: (_FakeDevAgent(router) if router is not None else None),
        wf_cfg=lambda: {"enabled": True},
        twin=lambda: twin,
        conv_mode=lambda: None,
        macro_store=lambda: None,
        agent_db=lambda: None,
        executor=lambda: None,
        tts_speak=_speak_and_suppress,
        speak_and_suppress=_speak_and_suppress,
    )
    wh._spoken = spoken
    return wh


def _cmd(text):
    return types.SimpleNamespace(text=text, source="voice", trace_id="t1")


async def test_happy_path_decomposes_fans_out_and_synthesizes():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    wh = _handler(runner, router)

    result = await wh.maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert result["spoken"] == "This is the synthesized spoken answer."
    assert result["subtasks"] == 3
    assert wh._spoken == ["This is the synthesized spoken answer."]
    # 1 decompose + 3 angles + 1 synthesis = 5 inference calls.
    assert len(router.calls) == 5


async def test_not_a_trigger_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    wh = _handler(runner, router)
    assert await wh.maybe_handle_workflow(_cmd("open chrome")) is None
    assert router.calls == []


async def test_disabled_runner_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=False)
    wh = _handler(runner, router)
    assert await wh.maybe_handle_workflow(_cmd("research transformers")) is None
    assert router.calls == []


async def test_flare_falls_through_to_ordinary_routing():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)

    class _Snap:
        pain_day_active = True

    class _Twin:
        async def get_snapshot(self):
            return _Snap()

    wh = _handler(runner, router, twin=_Twin())
    # A flare returns None (fall through) and issues no inference.
    assert await wh.maybe_handle_workflow(_cmd("research transformers")) is None
    assert router.calls == []


async def test_decompose_failure_degrades_to_single_angle():
    router = _FakeRouter(decompose_ok=False)
    runner = WorkflowRunner(router=router, enabled=True)
    wh = _handler(runner, router)

    result = await wh.maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["subtasks"] == 1   # single-angle fallback on the raw goal
    assert result["spoken"] == "This is the synthesized spoken answer."


async def test_all_subagents_failing_speaks_apology():
    router = _FakeRouter(angle_ok=False)
    runner = WorkflowRunner(router=router, enabled=True)
    wh = _handler(runner, router)

    result = await wh.maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert "couldn't" in result["spoken"].lower()
    assert wh._spoken and "couldn't" in wh._spoken[0].lower()


async def test_missing_router_returns_none():
    router = _FakeRouter()
    runner = WorkflowRunner(router=router, enabled=True)
    wh = _handler(runner, router=None)   # no dev agent / router wired
    assert await wh.maybe_handle_workflow(_cmd("research transformers")) is None
