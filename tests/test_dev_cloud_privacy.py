"""Tests for Gate-0 privacy on the dev-domain cloud path (audit fix Phase 2).

The dev pre-gate runs BEFORE the command-path Gate 0, so it must enforce Gate 0
itself: sensitive text (credentials/PII) must never reach CloudDevAgent, and an
allowed cloud dev call must scrub the query + recent-command context first. The
command-path cloud route must also scrub session_context (not just cmd.text).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator
from adaptive.content_filter import ContentFilter


_SECRET = "sk-ant-api03-" + "Z" * 40


def _coord_with_cloud_dev():
    """A coordinator wired to always route dev domains to a mock CloudDevAgent."""
    coord = HybridCoordinator()
    coord._content_filter = ContentFilter()
    coord._dev_agent = MagicMock()
    coord._dev_agent.handle = AsyncMock(return_value=MagicMock(
        domain="code", model_used="qwen3-coder:30b", response_text="local-answer",
        steps=[],
    ))
    cloud = MagicMock()
    cloud.model = "claude-opus-4-8"
    cloud.run = AsyncMock(return_value="cloud-answer")
    coord._cloud_dev_agent = cloud
    coord._cloud_always = True            # _should_route_cloud_dev → True
    # Force the domain classifier to a dev domain.
    coord._get_domain_classifier = lambda: MagicMock(classify=lambda t: "code")
    return coord, cloud


def test_sensitive_dev_query_forced_local_never_reaches_cloud():
    coord, cloud = _coord_with_cloud_dev()
    cmd = Command(text=f"write a script that uses my api key {_SECRET}",
                  action="CLARIFY", source="voice")
    result = asyncio.run(coord.route(cmd))

    cloud.run.assert_not_awaited()                    # never sent to cloud
    coord._dev_agent.handle.assert_awaited_once()     # forced to local DevAgent
    assert result["backend"] == "local"


def test_allowed_dev_query_is_scrubbed_before_cloud():
    coord, cloud = _coord_with_cloud_dev()
    # Pre-seed recent dev commands incl. a secret-bearing one.
    coord._recent_dev_commands = [f"earlier cmd with {_SECRET}", "harmless cmd"]
    cmd = Command(text="refactor the auth module", action="CLARIFY", source="voice")
    result = asyncio.run(coord.route(cmd))

    cloud.run.assert_awaited_once()
    sent_text, sent_domain, sent_ctx = cloud.run.await_args.args
    assert sent_text == "refactor the auth module"     # no secret here, unchanged
    # The secret in recent-command context must be redacted.
    joined = "\n".join(sent_ctx["recent_commands"])
    assert _SECRET not in joined
    assert "REDACTED" in joined
    assert result["backend"] == "anthropic_cloud"


def test_secret_in_dev_query_text_redacted_when_gate0_misses():
    """Defense in depth: even a secret Gate 0's keyword list doesn't catch (a
    raw key with no 'api key' phrase) is scrubbed by ContentFilter before egress."""
    coord, cloud = _coord_with_cloud_dev()
    cmd = Command(text=f"deploy with {_SECRET}", action="CLARIFY", source="voice")
    asyncio.run(coord.route(cmd))

    cloud.run.assert_awaited_once()
    sent_text = cloud.run.await_args.args[0]
    assert _SECRET not in sent_text
    assert "REDACTED" in sent_text


# ---------------------------------------------------------------------------
# Command-path cloud route scrubs session_context (not just cmd.text)
# ---------------------------------------------------------------------------

class _CloudSpy:
    def __init__(self):
        self.received = None

    async def infer(self, cmd):
        self.received = cmd
        return "CLICK ok"


def test_run_cloud_scrubs_session_context():
    coord = HybridCoordinator()
    coord._content_filter = ContentFilter()
    coord._cloud = _CloudSpy()

    cmd = Command(
        text="open the file", action="OPEN", source="voice",
        session_context=[f"earlier: my key is {_SECRET}", "later: open editor"],
    )
    out = asyncio.run(coord._run_cloud(cmd))
    assert out == "CLICK ok"
    ctx = coord._cloud.received.session_context
    joined = "\n".join(ctx)
    assert _SECRET not in joined                       # prior-command secret scrubbed
    assert "REDACTED" in joined


def test_run_cloud_clean_context_passes_through():
    coord = HybridCoordinator()
    coord._content_filter = ContentFilter()
    coord._cloud = _CloudSpy()

    cmd = Command(text="open the file", action="OPEN", source="voice",
                  session_context=["earlier: scroll down", "later: open editor"])
    asyncio.run(coord._run_cloud(cmd))
    assert coord._cloud.received.session_context == [
        "earlier: scroll down", "later: open editor",
    ]
