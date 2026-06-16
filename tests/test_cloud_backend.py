"""Offline tests for the cloud backend selector (Anthropic vs Amazon Bedrock).

No network: every test either inspects the resolved CloudBackend or patches the
SDK client constructor. Bedrock uses the classic AnthropicBedrock InvokeModel
client with cross-region inference-profile model ids.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import cloud_backend as cb
from core.hybrid_coordinator import _CloudInference

_HAIKU_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_OPUS_BEDROCK = "us.anthropic.claude-opus-4-8"


@pytest.fixture
def clean_env(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
              "DA_CLOUD_BACKEND", "DA_BEDROCK_REGION", "AWS_REGION",
              "DA_BEDROCK_PROFILE_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


# --- backend selection -----------------------------------------------------

def test_bedrock_auto_selected_when_key_present(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    assert cb.bedrock_selected() is True


def test_bedrock_not_selected_without_key(clean_env):
    assert cb.bedrock_selected() is False


def test_force_anthropic_overrides_bedrock_key(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    clean_env.setenv("DA_CLOUD_BACKEND", "anthropic")
    assert cb.bedrock_selected() is False


def test_force_bedrock_without_key_raises(clean_env):
    clean_env.setenv("DA_CLOUD_BACKEND", "bedrock")
    with pytest.raises(RuntimeError) as exc:
        cb.resolve_backend()
    assert "AWS_BEARER_TOKEN_BEDROCK" in str(exc.value)


# --- resolved backend shape ------------------------------------------------

def test_resolve_bedrock_backend(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    clean_env.setenv("DA_BEDROCK_REGION", "us-east-1")
    be = cb.resolve_backend()
    assert be.is_bedrock
    assert be.name == "bedrock:us-east-1"
    assert be.aws_region == "us-east-1"
    assert be.api_key == "bedrock-key"
    assert be.profile_prefix == "us"          # default


def test_bedrock_region_defaults_to_us_east_1(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    assert cb.resolve_backend().name == "bedrock:us-east-1"


def test_resolve_anthropic_backend(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    be = cb.resolve_backend()
    assert not be.is_bedrock
    assert be.name == "anthropic"
    assert be.api_key is None                 # SDK resolves env itself


def test_resolve_anthropic_explicit_key_override(clean_env):
    be = cb.resolve_backend(api_key="sk-ant-explicit")
    assert not be.is_bedrock
    assert be.api_key == "sk-ant-explicit"


# --- model id mapping ------------------------------------------------------

def test_map_model_bedrock_inference_profile(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", is_bedrock=True, profile_prefix="us")
    assert be.map_model("claude-haiku-4-5") == _HAIKU_BEDROCK
    assert be.map_model("claude-opus-4-8") == _OPUS_BEDROCK


def test_map_model_bedrock_global_prefix(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", is_bedrock=True, profile_prefix="global")
    assert be.map_model("claude-opus-4-8") == "global.anthropic.claude-opus-4-8"


def test_map_model_anthropic_unchanged(clean_env):
    be = cb.CloudBackend(name="anthropic", is_bedrock=False)
    assert be.map_model("claude-haiku-4-5") == "claude-haiku-4-5"


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


def test_cloud_inference_uses_anthropic_when_no_bedrock(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cloud = _CloudInference(model="claude-haiku-4-5")
    with patch("anthropic.Anthropic", return_value=object()) as ctor:
        cloud._get_client()
    _, kwargs = ctor.call_args
    assert "aws_region" not in kwargs          # first-party client, not Bedrock
    assert cloud._effective_model == "claude-haiku-4-5"
    assert cloud._backend == "anthropic"


# --- CloudDevAgent wiring (dev path) ---------------------------------------

def test_cloud_dev_agent_default_maps_to_sonnet_on_bedrock(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent()   # default model — Sonnet 4.6
    with patch("anthropic.AsyncAnthropicBedrock", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs["aws_region"] == "us-east-1"
    assert kwargs["api_key"] == "bedrock-key"
    assert kwargs["timeout"] == agent._timeout
    assert agent.model == "us.anthropic.claude-sonnet-4-6"
    assert agent._backend == "bedrock:us-east-1"


def test_cloud_dev_agent_explicit_key_takes_anthropic_path(clean_env):
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent(model="claude-opus-4-8", api_key="sk-ant-explicit")
    with patch("anthropic.AsyncAnthropic", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs.get("api_key") == "sk-ant-explicit"
    assert "aws_region" not in kwargs
    assert agent.model == "claude-opus-4-8"
