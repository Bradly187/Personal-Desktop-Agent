"""Personal-KB routing — DevAgent personal queries, the SEARCH_PERSONAL plan
verb, and the coordinator's help / index-my-notes / force-local behavior.

Run:
    python -m pytest tests/test_personal_query.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from inference.dev_agent import DevAgent, AgentStep
from core.hybrid_coordinator import HybridCoordinator
from core.voice_system_control import _is_system_control_voice
from core.voice_system_control import VoiceSystemControl


@pytest.fixture(autouse=True)
def _silence_tts(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(
        speak=AsyncMock(), speak_sync=lambda *_: True))


def _kb(hits):
    kb = MagicMock()
    kb.available = True
    kb.query = AsyncMock(return_value=hits)
    return kb


_HIT = {"file": "C:/notes/health.md", "name": "Doctor", "score": 0.9,
        "text": "Discussed methotrexate dosage on Tuesday."}


# ---------------------------------------------------------------------------
# DevAgent — single-turn personal query
# ---------------------------------------------------------------------------

async def test_personal_query_routes_to_kb_and_synthesises():
    router = MagicMock()
    router.infer = AsyncMock(return_value=MagicMock(
        ok=True, text="You discussed methotrexate dosage.", model="gemma4:12b"))
    agent = DevAgent(router=router)
    agent._personal_kb = _kb([_HIT])

    res = await agent.handle("what did I write in my notes about my doctor")
    assert res.domain == "personal"
    assert "methotrexate" in res.response_text
    # Synthesis stays LOCAL (domain=general → local model)
    assert router.infer.await_args.kwargs["domain"] == "general"


async def test_personal_query_no_hits_is_honest():
    router = MagicMock(); router.infer = AsyncMock()
    agent = DevAgent(router=router)
    agent._personal_kb = _kb([])
    res = await agent.handle("did I write anything in my notes about quantum chess")
    assert "couldn't find" in res.response_text.lower()
    router.infer.assert_not_awaited()      # nothing retrieved → no model call


async def test_non_personal_query_skips_kb():
    router = MagicMock()
    router.infer = AsyncMock(return_value=MagicMock(
        ok=True, text="def f(): ...", model="qwen3-coder:30b", error=None))
    agent = DevAgent(router=router)
    agent._persist_run = AsyncMock()
    kb = _kb([_HIT])
    agent._personal_kb = kb
    agent._rag_context = AsyncMock(return_value=None)
    await agent.handle("write a python function to reverse a string please")
    kb.query.assert_not_awaited()


async def test_synthesis_failure_speaks_short_not_raw_excerpts(monkeypatch):
    # Privacy: on local-synthesis failure the raw excerpts stay in
    # response_text but are NEVER sent to TTS (default backend is AWS Polly).
    spoke = AsyncMock()
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(speak=spoke))
    router = MagicMock()
    router.infer = AsyncMock(side_effect=RuntimeError("model down"))
    agent = DevAgent(router=router)
    agent._personal_kb = _kb([_HIT])
    res = await agent.handle("what's in my notes about my doctor")
    assert "RETRIEVED_CONTEXT" in res.response_text          # full data on screen
    # The speak coroutine is created synchronously even though the task is
    # fire-and-forget — call_args is recorded at call time, not await time.
    spoken_text = spoke.call_args.args[0]
    assert "RETRIEVED_CONTEXT" not in spoken_text            # never spoken
    assert "methotrexate" not in spoken_text                 # no content egress
    assert "couldn't summarize" in spoken_text


# ---------------------------------------------------------------------------
# SEARCH_PERSONAL plan verb
# ---------------------------------------------------------------------------

async def test_search_personal_step_fenced():
    agent = DevAgent(router=MagicMock())
    agent._personal_kb = _kb([_HIT])
    out = await agent._execute_step(AgentStep(action="SEARCH_PERSONAL", args="doctor"))
    assert "RETRIEVED_CONTEXT" in out and "methotrexate" in out


async def test_search_personal_without_kb():
    agent = DevAgent(router=MagicMock())
    out = await agent._execute_step(AgentStep(action="SEARCH_PERSONAL", args="x"))
    assert "not available" in out.lower()


# ---------------------------------------------------------------------------
# Coordinator — help, index-my-notes, force-local
# ---------------------------------------------------------------------------

def _coord() -> VoiceSystemControl:
    box = {"skill_registry": None, "personal_kb": None}

    async def _noop(*a, **k):
        return None

    vsc = VoiceSystemControl(
        whisper=lambda: None, agent_db=lambda: None, twin=lambda: None,
        profiler=lambda: None, calibrator=lambda: None, dev_agent=lambda: None,
        personal_kb=lambda: box["personal_kb"],
        skill_registry=lambda: box["skill_registry"],
        audit=lambda: None, tts_speak=_noop, approval_config=lambda: {},
        switch_condition=_noop, run_calibration=_noop,
    )
    vsc._box = box  # test-only handle for mutating skill_registry/personal_kb
    return vsc


def test_help_summary_includes_dynamic_skills_and_kb():
    c = _coord()
    reg = MagicMock()
    reg.has_skills = MagicMock(return_value=True)
    reg.list_actions = MagicMock(return_value=[
        {"keywords": ["next meeting"]}, {"keywords": ["unread email"]}])
    c._box["skill_registry"] = reg
    c._box["personal_kb"] = MagicMock(available=True)
    s = c._capability_summary()
    assert "next meeting" in s and "your own documents" in s and "reminders" in s


def test_help_summary_without_extras():
    s = _coord()._capability_summary()
    assert "click" in s.lower() and "reminders" in s.lower()


def test_help_and_index_phrases_are_system_control():
    cmd = MagicMock()
    cmd.source = "voice"
    for phrase in ("help", "what can you do", "index my notes"):
        cmd.text = phrase
        assert _is_system_control_voice(cmd), phrase


def test_personal_query_is_not_misrouted_as_system_control():
    cmd = MagicMock()
    cmd.source = "voice"
    cmd.text = "what did I write in my notes about my doctor"
    assert not _is_system_control_voice(cmd)


def test_gate0_forces_personal_queries_local():
    # Covers the COMMAND path too ("open my notes about X" classifies command;
    # Gates 1–4 could otherwise escalate the text to cloud).
    from core.hybrid_coordinator import CoordinatorConfig
    from core.gate_evaluator import GateEvaluator
    gates = GateEvaluator(
        CoordinatorConfig(), run_local=None, run_cloud=None,
        approval_config=lambda: {})
    cmd = MagicMock()
    cmd.text = "open my notes about the new medication"
    assert gates.gate0(cmd) is False                  # personal → never cloud
    cmd.text = "open the web browser"
    assert gates.gate0(cmd) is True                   # ordinary command unaffected
    # The guard must hold even with keyword Gate-0 disabled.
    gates._cfg = CoordinatorConfig(gate0_enabled=False)
    cmd.text = "open my notes about the new medication"
    assert gates.gate0(cmd) is False
