"""Regression: HybridCoordinator must expose _get_domain_classifier().

The dev-agent pre-gate in EventDispatcher.route_impl calls
`self._coordinator._get_domain_classifier().classify(cmd.text)` on EVERY
non-system-control command. The god-object refactor (PR #164) kept the
`_domain_classifier = None` class attribute on HybridCoordinator but dropped the
accessor method, moving it only onto CorrectionHandler — so the first dev-domain
command crashed with:

    'HybridCoordinator' object has no attribute '_get_domain_classifier'

The privacy tests in test_dev_cloud_privacy.py missed this because they
monkeypatch `coord._get_domain_classifier = lambda: MagicMock(...)`. These tests
deliberately exercise the REAL accessor so a future removal is caught again.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator


def test_coordinator_exposes_domain_classifier_accessor():
    """The accessor exists on the coordinator and returns a real classifier."""
    dc = HybridCoordinator._get_domain_classifier()
    assert hasattr(dc, "classify")
    # Stateless class-level cache: same instance every call.
    assert HybridCoordinator._get_domain_classifier() is dc
    assert dc.classify("refactor the auth module") == "code"


def test_dev_pregate_uses_real_classifier_without_error():
    """route() reaches the local DevAgent branch through the real accessor.

    No monkeypatch of _get_domain_classifier — this is the exact call the
    regression broke. A dev-domain command ("refactor ...") must reach
    _dev_agent.handle rather than raising AttributeError at the pre-gate.
    """
    coord = HybridCoordinator()
    coord._dev_agent = MagicMock()
    coord._dev_agent.handle = AsyncMock(return_value=MagicMock(
        domain="code", model_used="qwen3-coder:30b",
        response_text="ok", steps=[],
    ))

    result = asyncio.run(coord.route(
        Command(text="refactor the auth module", action="CLARIFY", source="voice")
    ))

    coord._dev_agent.handle.assert_awaited_once()
    assert result["action"] == "dev_agent"
    assert result["backend"] == "local"
