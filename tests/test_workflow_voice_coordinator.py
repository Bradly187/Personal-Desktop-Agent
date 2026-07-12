"""Integration tests for the workflow voice trigger glue in WorkflowHandler.

These exercise ``maybe_handle_workflow`` end-to-end to ensure it delegates
correctly to the monolithic dev_agent.plan_and_run.
"""

import types

import pytest

from core.workflow_handler import WorkflowHandler

pytestmark = pytest.mark.asyncio


class _FakeResult:
    def __init__(self, ok=True, text=""):
        self.ok = ok
        self.text = text

class _FakeRouter:
    def __init__(self, dev_agent):
        self.dev_agent = dev_agent

    async def infer(self, domain, user_text, context):
        self.dev_agent.calls.append(user_text)
        if not self.dev_agent._ok:
            return _FakeResult(ok=False)
        return _FakeResult(ok=True, text="This is the spoken answer.")

class _FakeDevAgent:
    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok
        self._router = _FakeRouter(self)


def _handler(dev_agent, *, twin=None):
    spoken: list = []

    async def _speak_and_suppress(text):
        spoken.append(text)

    wh = WorkflowHandler(
        dev_agent=lambda: dev_agent,
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


async def test_happy_path_runs_plain_inference():
    dev_agent = _FakeDevAgent()
    wh = _handler(dev_agent)

    result = await wh.maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert result["spoken"] == "This is the spoken answer."
    assert wh._spoken == ["This is the spoken answer."]
    assert dev_agent.calls == ["transformers"]


async def test_not_a_trigger_returns_none():
    dev_agent = _FakeDevAgent()
    wh = _handler(dev_agent)
    assert await wh.maybe_handle_workflow(_cmd("open chrome")) is None
    assert dev_agent.calls == []


async def test_flare_falls_through_to_ordinary_routing():
    dev_agent = _FakeDevAgent()

    class _Snap:
        pain_day_active = True

    class _Twin:
        async def get_snapshot(self):
            return _Snap()

    wh = _handler(dev_agent, twin=_Twin())
    # A flare returns None (fall through) and issues no inference.
    assert await wh.maybe_handle_workflow(_cmd("research transformers")) is None
    assert dev_agent.calls == []


async def test_dev_agent_failure_speaks_apology():
    dev_agent = _FakeDevAgent(ok=False)
    wh = _handler(dev_agent)

    result = await wh.maybe_handle_workflow(_cmd("research transformers"))

    assert result is not None
    assert result["action"] == "WORKFLOW"
    assert "couldn't" in result["spoken"].lower()
    assert wh._spoken and "couldn't" in wh._spoken[0].lower()


async def test_missing_dev_agent_returns_none():
    wh = _handler(None)
    assert await wh.maybe_handle_workflow(_cmd("research transformers")) is None
