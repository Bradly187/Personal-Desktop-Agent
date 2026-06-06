"""Tests for CloudDevAgent — Anthropic-API dev-domain fallback path.

All tests inject a fake async Anthropic client (no network) by setting the
agent's lazily-constructed ``_client`` directly, so ``_get_client`` returns the
fake without touching the real SDK or any API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.cloud_dev_agent import (  # noqa: E402
    CloudDevAgent,
    _extract_paths,
    _extract_text,
    _SYSTEM_PROMPTS,
)


# ---------------------------------------------------------------------------
# Fakes — mimic the shape used by run(): client.messages.stream(...) async ctx
# whose get_final_message() returns a Message with .content / .stop_reason / .usage
# ---------------------------------------------------------------------------

class _FakeBlock:
    def __init__(self, type: str, text: str = "") -> None:
        self.type = type
        self.text = text


class _FakeMessage:
    def __init__(self, blocks, stop_reason="end_turn", usage=None) -> None:
        self.content = blocks
        self.stop_reason = stop_reason
        self.usage = usage


class _FakeStreamCtx:
    def __init__(self, message) -> None:
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, message=None, raise_exc=None) -> None:
        self._message = message
        self._raise = raise_exc
        self.last_kwargs: dict = {}

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        return _FakeStreamCtx(self._message)


class _FakeClient:
    def __init__(self, message=None, raise_exc=None) -> None:
        self.messages = _FakeMessages(message, raise_exc)


def _agent_with_text(text: str) -> tuple[CloudDevAgent, _FakeClient]:
    agent = CloudDevAgent(api_key="test-key")
    client = _FakeClient(message=_FakeMessage([_FakeBlock("text", text)]))
    agent._client = client  # inject — bypasses _get_client's SDK/key checks
    return agent, client


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_extract_text_drops_non_text_blocks():
    msg = _FakeMessage([
        _FakeBlock("thinking", "secret reasoning"),
        _FakeBlock("text", "hello "),
        _FakeBlock("text", "world"),
    ])
    assert _extract_text(msg) == "hello world"


def test_extract_paths_finds_windows_posix_and_bare():
    text = "Edit E:\\proj\\main.py and core/foo.py, then check bar.swift"
    paths = _extract_paths(text)
    assert "E:\\proj\\main.py" in paths
    assert "core/foo.py" in paths
    assert "bar.swift" in paths


def test_extract_paths_empty():
    assert _extract_paths("just some plain text with no files") == []


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_shape():
    agent = CloudDevAgent(api_key="k", model="claude-opus-4-8")
    st = agent.get_status()
    assert set(st) == {"available", "model", "domain_count", "backend"}
    assert st["backend"] == "anthropic_cloud"
    assert st["model"] == "claude-opus-4-8"
    assert st["domain_count"] == len(_SYSTEM_PROMPTS)
    assert st["available"] is True  # key provided


def test_get_status_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = CloudDevAgent(api_key=None)
    assert agent.get_status()["available"] is False


# ---------------------------------------------------------------------------
# run() — success paths
# ---------------------------------------------------------------------------

async def test_run_returns_text_and_uses_domain_prompt():
    agent, client = _agent_with_text("def f(): pass")
    out = await agent.run("write a function", domain="code")
    assert out == "def f(): pass"
    kw = client.messages.last_kwargs
    assert kw["model"] == "claude-opus-4-8"
    assert kw["system"][0]["text"] == _SYSTEM_PROMPTS["code"]
    # code is not a thinking domain
    assert kw["thinking"] == {"type": "disabled"}


async def test_run_math_enables_adaptive_thinking():
    agent, client = _agent_with_text("the answer")
    await agent.run("integrate x^2", domain="math")
    assert client.messages.last_kwargs["thinking"] == {"type": "adaptive"}


async def test_run_unknown_domain_falls_back_to_general():
    agent, client = _agent_with_text("ok")
    await agent.run("hello", domain="banana")
    assert client.messages.last_kwargs["system"][0]["text"] == _SYSTEM_PROMPTS["general"]


async def test_run_includes_context_preamble():
    agent, client = _agent_with_text("ok")
    await agent.run(
        "refactor core/foo.py",
        domain="code",
        context={"session_id": 42, "recent_commands": ["open vscode", "run tests"]},
    )
    user_text = client.messages.last_kwargs["messages"][0]["content"][-1]["text"]
    assert "Session ID: 42" in user_text
    assert "run tests" in user_text
    assert "core/foo.py" in user_text  # extracted path
    assert "[Request]" in user_text


async def test_run_vision_attaches_screenshot():
    agent, client = _agent_with_text("a terminal")
    await agent.run("what's on screen?", domain="vision",
                    context={"screenshot_b64": "ZmFrZQ=="})
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == "ZmFrZQ=="


async def test_run_no_text_block_returns_clarify():
    agent = CloudDevAgent(api_key="k")
    agent._client = _FakeClient(message=_FakeMessage([_FakeBlock("thinking", "...")]))
    out = await agent.run("hi", domain="general")
    assert out.startswith("CLARIFY")


# ---------------------------------------------------------------------------
# run() — error contract
# ---------------------------------------------------------------------------

async def test_run_api_error_returns_clarify_string():
    agent = CloudDevAgent(api_key="k")
    agent._client = _FakeClient(raise_exc=RuntimeError("boom"))
    out = await agent.run("write code", domain="code")
    assert out == "CLARIFY cloud dev agent temporarily unavailable"


async def test_run_missing_key_returns_clarify(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = CloudDevAgent(api_key=None)  # no client injected → _get_client raises
    out = await agent.run("write code", domain="code")
    assert out == "CLARIFY cloud dev agent temporarily unavailable"
