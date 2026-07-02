"""Offline tests for the cloud backend selector (Amazon Bedrock only).

This project accesses Claude exclusively through Amazon Bedrock — there is no
direct first-party Anthropic API path and no ANTHROPIC_API_KEY dependency. No
network: every test either inspects the resolved CloudBackend or patches the SDK
client constructor. Bedrock uses the classic AnthropicBedrock InvokeModel client
with cross-region inference-profile model ids.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import cloud_backend as cb
from core.inference_runner import _CloudInference

_HAIKU_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_OPUS_BEDROCK = "us.anthropic.claude-opus-4-8"


@pytest.fixture
def clean_env(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
              "DA_CLOUD_BACKEND", "DA_BEDROCK_REGION", "AWS_REGION",
              "DA_BEDROCK_PROFILE_PREFIX", "DA_CLOUD_DEV_MODEL"):
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


# --- credential availability -----------------------------------------------

def test_credential_available_with_bedrock_key(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    assert cb.credential_available() is True


def test_credential_unavailable_without_bedrock_key(clean_env):
    assert cb.credential_available() is False


def test_anthropic_api_key_does_not_enable_cloud(clean_env):
    """ANTHROPIC_API_KEY is no longer a cloud credential — only Bedrock is."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert cb.credential_available() is False


# --- resolved backend shape ------------------------------------------------

def test_resolve_bedrock_backend(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    clean_env.setenv("DA_BEDROCK_REGION", "us-east-1")
    be = cb.resolve_backend()
    assert be.name == "bedrock:us-east-1"
    assert be.aws_region == "us-east-1"
    assert be.api_key == "bedrock-key"
    assert be.profile_prefix == "us"          # default


def test_bedrock_region_defaults_to_us_east_1(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    assert cb.resolve_backend().name == "bedrock:us-east-1"


def test_missing_bedrock_key_raises_actionable(clean_env):
    with pytest.raises(RuntimeError) as exc:
        cb.resolve_backend()
    assert "AWS_BEARER_TOKEN_BEDROCK" in str(exc.value)


def test_anthropic_key_alone_still_raises(clean_env):
    """A first-party key present but no Bedrock token must still raise."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    with pytest.raises(RuntimeError) as exc:
        cb.resolve_backend()
    assert "AWS_BEARER_TOKEN_BEDROCK" in str(exc.value)


# --- model id mapping ------------------------------------------------------

def test_map_model_bedrock_inference_profile(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", aws_region="us-east-1",
                         api_key="k", profile_prefix="us")
    assert be.map_model("claude-haiku-4-5") == _HAIKU_BEDROCK
    assert be.map_model("claude-opus-4-8") == _OPUS_BEDROCK


def test_map_model_bedrock_global_prefix(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", aws_region="us-east-1",
                         api_key="k", profile_prefix="global")
    assert be.map_model("claude-opus-4-8") == "global.anthropic.claude-opus-4-8"


def test_map_model_unknown_alias_falls_back(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", aws_region="us-east-1",
                         api_key="k", profile_prefix="us")
    assert be.map_model("claude-future-9") == "us.anthropic.claude-future-9"


# --- _CloudInference wiring (command path) ---------------------------------

def test_cloud_inference_uses_bedrock_client_and_profile_id(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    cloud = _CloudInference(model="claude-haiku-4-5")
    with patch("anthropic.AnthropicBedrock", return_value=object()) as ctor:
        cloud._get_client()
    _, kwargs = ctor.call_args
    assert kwargs["aws_region"] == "us-east-1"
    assert kwargs["api_key"] == "bedrock-key"
    assert cloud._effective_model == _HAIKU_BEDROCK
    assert cloud._backend == "bedrock:us-east-1"


def test_cloud_inference_raises_without_bedrock_key(clean_env):
    """No Bedrock token → infer() degrades to CLARIFY, never the raw SDK error."""
    import asyncio
    from core.command_executor import Command
    cloud = _CloudInference(model="claude-haiku-4-5")
    out = asyncio.run(cloud.infer(Command(text="open chrome", action="OPEN", source="voice")))
    assert out.startswith("CLARIFY")
    assert "AWS_BEARER_TOKEN_BEDROCK" in out


# --- CloudDevAgent wiring (dev path) ---------------------------------------

def test_cloud_dev_agent_default_maps_to_opus_on_bedrock(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent()   # default model — Opus 4.8
    with patch("anthropic.AsyncAnthropicBedrock", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs["aws_region"] == "us-east-1"
    assert kwargs["api_key"] == "bedrock-key"
    assert kwargs["timeout"] == agent._timeout
    assert agent.model == _OPUS_BEDROCK
    assert agent._backend == "bedrock:us-east-1"


def test_cloud_dev_agent_explicit_key_is_ignored_bedrock_wins(clean_env):
    """An explicit constructor api_key is ignored — Bedrock is the only backend."""
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent(model="claude-opus-4-8", api_key="sk-ant-explicit")
    with patch("anthropic.AsyncAnthropicBedrock", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs["api_key"] == "bedrock-key"   # env token wins, not the arg
    assert kwargs["aws_region"] == "us-east-1"
    assert agent.model == _OPUS_BEDROCK
