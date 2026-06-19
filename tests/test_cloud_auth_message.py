"""Offline regression tests for the cloud fallback's auth-failure messaging.

The cloud path (_CloudInference) is only a Gate-2 safety net, but when it can't
authenticate it must degrade to a CLEAR, actionable CLARIFY — not the raw SDK
"Could not resolve authentication method" traceback that confused a real session.

These tests never touch the network: the missing-key path short-circuits before
any API call, and the invalid-key path is simulated with a fake client.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import _CloudInference


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # Ensure the direct-Anthropic path is the one under test (no Bedrock backend).
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("DA_CLOUD_BACKEND", raising=False)


def test_missing_key_get_client_raises_clear_message(no_key):
    cloud = _CloudInference(model="claude-haiku-4-5")
    with pytest.raises(RuntimeError) as exc:
        cloud._get_client()
    msg = str(exc.value)
    assert "ANTHROPIC_API_KEY not set" in msg
    assert "setx ANTHROPIC_API_KEY" in msg
    assert "Claude Max" in msg          # tells the user it's NOT covered by Max


def test_missing_key_infer_degrades_to_clarify(no_key):
    cloud = _CloudInference(model="claude-haiku-4-5")
    out = _run(cloud.infer(Command(text="open chrome", action="OPEN", source="voice")))
    assert out.startswith("CLARIFY")
    assert "ANTHROPIC_API_KEY" in out


def test_present_key_passes_env_check(monkeypatch):
    """A present key gets past the proactive check (client construction is mocked)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    # Pin the direct-Anthropic backend (this test asserts on anthropic.Anthropic);
    # a Bedrock token in the dev env would otherwise route to AnthropicBedrock.
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("DA_CLOUD_BACKEND", "anthropic")
    cloud = _CloudInference(model="claude-haiku-4-5")
    with patch("anthropic.Anthropic", return_value=object()) as ctor:
        client = cloud._get_client()
    assert client is not None
    ctor.assert_called_once()


def test_invalid_key_runtime_auth_error_is_friendly(monkeypatch):
    """A 401 at request time surfaces an actionable message, not the raw SDK error."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
    cloud = _CloudInference(model="claude-haiku-4-5")

    class _FakeAuthError(Exception):
        status_code = 401

    _FakeAuthError.__name__ = "AuthenticationError"

    class _FakeClient:
        class messages:
            @staticmethod
            def create(**_kw):
                raise _FakeAuthError("401 invalid x-api-key")

    with patch.object(cloud, "_get_client", return_value=_FakeClient()):
        out = _run(cloud.infer(Command(text="open chrome", action="OPEN", source="voice")))
    assert out.startswith("CLARIFY cloud auth failed")
    assert "console.anthropic.com" in out
