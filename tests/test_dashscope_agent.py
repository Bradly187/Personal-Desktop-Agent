"""Tests for DashScopeAgent — Qwen cloud dev path via DashScope OpenAI API.

All tests inject a fake async OpenAI client (no network) by setting the agent's
lazily-constructed ``_client`` directly, so ``_get_client`` returns the fake
without touching the real SDK or any API key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dashscope_agent import (  # noqa: E402
    DashScopeAgent,
    _DOMAIN_MODELS,
    _SYSTEM_PROMPTS,
    _extract_paths,
    _strip_thinking,
)


# ---------------------------------------------------------------------------
# Fakes — mimic openai.AsyncOpenAI chat.completions.create() response shape
# ---------------------------------------------------------------------------

def _make_completion(content: str, finish_reason: str = "stop"):
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(total_tokens=42)
    return SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
    def __init__(self, content=None, raise_exc=None) -> None:
        self._content = content
        self._raise = raise_exc
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return _make_completion(self._content or "")


class _FakeChat:
    def __init__(self, content=None, raise_exc=None) -> None:
        self.completions = _FakeCompletions(content, raise_exc)


class _FakeClient:
    def __init__(self, content=None, raise_exc=None) -> None:
        self.chat = _FakeChat(content, raise_exc)


def _agent_with_response(text: str) -> tuple[DashScopeAgent, _FakeClient]:
    agent = DashScopeAgent(api_key="test-key")
    client = _FakeClient(content=text)
    agent._client = client
    return agent, client


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_strip_thinking_removes_think_block():
    raw = "<think>internal reasoning here</think>final answer"
    assert _strip_thinking(raw) == "final answer"


def test_strip_thinking_multiline():
    raw = "<think>\nstep 1\nstep 2\n</think>\nresult"
    assert _strip_thinking(raw) == "result"


def test_strip_thinking_no_block_unchanged():
    assert _strip_thinking("just an answer") == "just an answer"


def test_extract_paths_windows_and_posix():
    text = "edit E:\\proj\\main.py and core/foo.py then bar.swift"
    paths = _extract_paths(text)
    assert "E:\\proj\\main.py" in paths
    assert "core/foo.py" in paths
    assert "bar.swift" in paths


def test_extract_paths_empty():
    assert _extract_paths("no files here") == []


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_shape():
    agent = DashScopeAgent(api_key="k")
    st = agent.get_status()
    assert set(st) == {"available", "models", "domain_count", "backend"}
    assert st["backend"] == "dashscope"
    assert st["domain_count"] == len(_SYSTEM_PROMPTS)
    assert st["available"] is True


def test_get_status_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    agent = DashScopeAgent(api_key=None)
    assert agent.get_status()["available"] is False


def test_model_property_contains_domain_models():
    agent = DashScopeAgent(api_key="k")
    for m in set(_DOMAIN_MODELS.values()):
        assert m in agent.model


# ---------------------------------------------------------------------------
# run() — success paths
# ---------------------------------------------------------------------------

async def test_run_returns_text():
    agent, _ = _agent_with_response("def f(): pass")
    out = await agent.run("write a function", domain="code")
    assert out == "def f(): pass"


async def test_run_uses_domain_model():
    agent, client = _agent_with_response("answer")
    await agent.run("question", domain="math")
    assert client.chat.completions.last_kwargs["model"] == _DOMAIN_MODELS["math"]


async def test_run_code_uses_code_model():
    agent, client = _agent_with_response("code")
    await agent.run("write code", domain="code")
    assert client.chat.completions.last_kwargs["model"] == _DOMAIN_MODELS["code"]


async def test_run_vision_uses_vision_model():
    agent, client = _agent_with_response("a terminal")
    await agent.run("what's on screen?", domain="vision")
    assert client.chat.completions.last_kwargs["model"] == _DOMAIN_MODELS["vision"]


async def test_run_unknown_domain_falls_back_to_general():
    agent, client = _agent_with_response("ok")
    await agent.run("hello", domain="banana")
    assert client.chat.completions.last_kwargs["model"] == _DOMAIN_MODELS["general"]


async def test_run_system_prompt_in_messages():
    agent, client = _agent_with_response("ok")
    await agent.run("explain", domain="code")
    msgs = client.chat.completions.last_kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == _SYSTEM_PROMPTS["code"]


async def test_run_strips_qwq_thinking_tags():
    agent, _ = _agent_with_response("<think>reasoning</think>final answer")
    out = await agent.run("solve", domain="math")
    assert out == "final answer"
    assert "<think>" not in out


async def test_run_includes_context_preamble():
    agent, client = _agent_with_response("ok")
    await agent.run(
        "refactor core/foo.py",
        domain="code",
        context={"session_id": 7, "recent_commands": ["open vscode", "run tests"]},
    )
    msgs = client.chat.completions.last_kwargs["messages"]
    user_text = msgs[1]["content"][-1]["text"]
    assert "Session ID: 7" in user_text
    assert "run tests" in user_text
    assert "core/foo.py" in user_text
    assert "[Request]" in user_text


async def test_run_vision_attaches_image_url():
    agent, client = _agent_with_response("a terminal")
    await agent.run(
        "what's on screen?", domain="vision",
        context={"screenshot_b64": "ZmFrZQ=="},
    )
    msgs = client.chat.completions.last_kwargs["messages"]
    user_content = msgs[1]["content"]
    assert user_content[0]["type"] == "image_url"
    assert "ZmFrZQ==" in user_content[0]["image_url"]["url"]


async def test_run_no_screenshot_no_image_block():
    agent, client = _agent_with_response("ok")
    await agent.run("what's on screen?", domain="vision")
    msgs = client.chat.completions.last_kwargs["messages"]
    user_content = msgs[1]["content"]
    types = [b["type"] for b in user_content]
    assert "image_url" not in types


async def test_run_empty_response_returns_clarify():
    agent, _ = _agent_with_response("")
    out = await agent.run("hi", domain="general")
    assert out.startswith("CLARIFY")


# ---------------------------------------------------------------------------
# run() — error contract
# ---------------------------------------------------------------------------

async def test_run_api_error_returns_clarify():
    agent = DashScopeAgent(api_key="k")
    agent._client = _FakeClient(raise_exc=RuntimeError("boom"))
    out = await agent.run("write code", domain="code")
    assert out == "CLARIFY Qwen cloud dev agent temporarily unavailable"


async def test_run_missing_key_returns_clarify(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    agent = DashScopeAgent(api_key=None)
    out = await agent.run("write code", domain="code")
    assert out == "CLARIFY Qwen cloud dev agent temporarily unavailable"


async def test_run_error_does_not_expose_exception_details(monkeypatch):
    agent = DashScopeAgent(api_key="k")
    agent._client = _FakeClient(raise_exc=RuntimeError("secret-api-key-in-error"))
    out = await agent.run("write code", domain="code")
    assert "secret-api-key-in-error" not in out


# ---------------------------------------------------------------------------
# HybridCoordinator integration — _cloud_agent_for_domain
# ---------------------------------------------------------------------------

def _make_coordinator():
    """Return a minimal HybridCoordinator with no DB/LLM deps."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from unittest.mock import MagicMock, AsyncMock

    # Patch heavy imports before coordinator loads
    for mod in ("storage.db", "adaptive.behavioral_twin_state",
                "storage.semantic_memory", "inference.local_inference"):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    from core.hybrid_coordinator import HybridCoordinator
    coord = HybridCoordinator.__new__(HybridCoordinator)
    # Minimal init of the attributes _cloud_agent_for_domain uses
    coord._cloud_dev_agent = None
    coord._cloud_always = False
    coord._local_specialist_available = None
    coord._dashscope_agent = None
    coord._dashscope_domains = frozenset({"math", "code", "vision"})
    return coord


def test_cloud_agent_none_when_neither_wired():
    coord = _make_coordinator()
    agent, label = coord._cloud_agent_for_domain("math")
    assert agent is None
    assert label is None


def test_dashscope_preferred_for_math():
    coord = _make_coordinator()
    ds_agent = DashScopeAgent(api_key="k")
    coord._dashscope_agent = ds_agent
    coord._cloud_always = True
    agent, label = coord._cloud_agent_for_domain("math")
    assert agent is ds_agent
    assert label == "dashscope"


def test_anthropic_used_for_plan_when_dashscope_wired():
    from unittest.mock import MagicMock
    coord = _make_coordinator()
    coord._dashscope_agent = DashScopeAgent(api_key="k")
    coord._cloud_dev_agent = MagicMock()
    coord._cloud_always = True
    agent, label = coord._cloud_agent_for_domain("plan")
    assert label == "anthropic_cloud"


def test_dashscope_catchall_when_anthropic_absent():
    coord = _make_coordinator()
    ds_agent = DashScopeAgent(api_key="k")
    coord._dashscope_agent = ds_agent
    coord._cloud_dev_agent = None
    coord._cloud_always = True
    agent, label = coord._cloud_agent_for_domain("plan")
    assert agent is ds_agent
    assert label == "dashscope"


def test_no_cloud_when_local_specialist_awake():
    coord = _make_coordinator()
    coord._dashscope_agent = DashScopeAgent(api_key="k")
    coord._local_specialist_available = lambda: True   # specialist awake
    agent, label = coord._cloud_agent_for_domain("math")
    assert agent is None


def test_cloud_fires_when_local_specialist_sleeping():
    coord = _make_coordinator()
    ds_agent = DashScopeAgent(api_key="k")
    coord._dashscope_agent = ds_agent
    coord._local_specialist_available = lambda: False  # specialist asleep
    agent, label = coord._cloud_agent_for_domain("math")
    assert agent is ds_agent
