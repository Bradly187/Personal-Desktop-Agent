"""Offline tests for the cloud backend selector (Anthropic vs Amazon Bedrock).

No network: every test either inspects the resolved CloudBackend or patches the
SDK client constructor. Bedrock is exercised via the documented bearer-token
Mantle path (standard client + base_url + api_key).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import cloud_backend as cb
from core.command_executor import Command
from core.hybrid_coordinator import _CloudInference


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clean_env(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
              "DA_CLOUD_BACKEND", "DA_BEDROCK_REGION", "AWS_REGION"):
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
    assert be.client_kwargs["api_key"] == "bedrock-key"
    assert be.client_kwargs["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/anthropic"


def test_bedrock_region_defaults_to_us_east_1(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    assert cb.resolve_backend().name == "bedrock:us-east-1"


def test_resolve_anthropic_backend(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    be = cb.resolve_backend()
    assert not be.is_bedrock
    assert be.name == "anthropic"
    assert be.client_kwargs == {}


# --- model id mapping ------------------------------------------------------

def test_map_model_bedrock_prefixes(clean_env):
    be = cb.CloudBackend(name="bedrock:us-east-1", is_bedrock=True)
    assert be.map_model("claude-haiku-4-5") == "anthropic.claude-haiku-4-5"
    assert be.map_model("claude-opus-4-8") == "anthropic.claude-opus-4-8"
    # idempotent — already-prefixed ids are left alone
    assert be.map_model("anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"


def test_map_model_anthropic_unchanged(clean_env):
    be = cb.CloudBackend(name="anthropic", is_bedrock=False)
    assert be.map_model("claude-haiku-4-5") == "claude-haiku-4-5"


# --- _CloudInference wiring (command path) ---------------------------------

def test_cloud_inference_uses_bedrock_model_and_endpoint(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    cloud = _CloudInference(model="claude-haiku-4-5")
    with patch("anthropic.Anthropic", return_value=object()) as ctor:
        cloud._get_client()
    # client built against the Mantle endpoint with the Bedrock key
    _, kwargs = ctor.call_args
    assert kwargs["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    assert kwargs["api_key"] == "bedrock-key"
    # request will use the Bedrock-prefixed model id
    assert cloud._effective_model == "anthropic.claude-haiku-4-5"
    assert cloud._backend == "bedrock:us-east-1"


def test_cloud_inference_uses_anthropic_when_no_bedrock(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cloud = _CloudInference(model="claude-haiku-4-5")
    with patch("anthropic.Anthropic", return_value=object()) as ctor:
        cloud._get_client()
    _, kwargs = ctor.call_args
    assert "base_url" not in kwargs           # first-party default endpoint
    assert cloud._effective_model == "claude-haiku-4-5"
    assert cloud._backend == "anthropic"


# --- CloudDevAgent wiring (dev path) ---------------------------------------

def test_cloud_dev_agent_maps_opus_to_bedrock(clean_env):
    clean_env.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent(model="claude-opus-4-8")
    with patch("anthropic.AsyncAnthropic", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    assert kwargs["api_key"] == "bedrock-key"
    assert agent.model == "anthropic.claude-opus-4-8"
    assert agent._backend == "bedrock:us-east-1"


def test_cloud_dev_agent_explicit_key_takes_anthropic_path(clean_env):
    from inference.cloud_dev_agent import CloudDevAgent
    agent = CloudDevAgent(model="claude-opus-4-8", api_key="sk-ant-explicit")
    with patch("anthropic.AsyncAnthropic", return_value=object()) as ctor:
        agent._get_client()
    _, kwargs = ctor.call_args
    assert kwargs.get("api_key") == "sk-ant-explicit"
    assert "base_url" not in kwargs
    assert agent.model == "claude-opus-4-8"
