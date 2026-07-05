"""Tests for BehavioralTwinState context namespacing — Gap 4 (AIOS alignment).

Covers:
- Dev-agent observations never touch PreferenceModel or SemanticMemory
- Accessibility observations update PreferenceModel normally
- Dev namespace auto-clears on get_session_context("dev_agent")
- clear_dev_namespace() empties dev history
- Accessibility session_context is unchanged after dev observations
- _session_history initialized as dict[str, list] with both keys
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cmd(text="click the button", source="voice"):
    cmd = MagicMock()
    cmd.text = text
    cmd.source = source
    cmd.whisper_logprob = -0.1
    cmd.gesture_confidence = 0.0
    cmd.params = {}
    return cmd


def _make_twin_state(db=None, semantic=None):
    """Create a BehavioralTwinState with mocked backing stores."""
    from adaptive.behavioral_twin_state import BehavioralTwinState

    if db is None:
        db = AsyncMock()
        db.available = False

    twin = BehavioralTwinState(agent_db=db)

    # Replace the SemanticMemory with a mock to avoid ChromaDB import
    mock_semantic = MagicMock()
    mock_semantic.available = False
    mock_semantic.add = AsyncMock()
    mock_semantic.query_similar = AsyncMock(return_value=[])
    twin._semantic_memory = mock_semantic

    return twin


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestSessionHistoryInit:
    def test_session_history_is_dict(self):
        twin = _make_twin_state()
        assert isinstance(twin._session_history, dict)

    def test_accessibility_key_present(self):
        twin = _make_twin_state()
        assert "accessibility" in twin._session_history

    def test_dev_agent_key_present(self):
        twin = _make_twin_state()
        assert "dev_agent" in twin._session_history

    def test_both_start_empty(self):
        twin = _make_twin_state()
        assert twin._session_history["accessibility"] == []
        assert twin._session_history["dev_agent"] == []


# ---------------------------------------------------------------------------
# Dev namespace isolation
# ---------------------------------------------------------------------------

class TestDevNamespaceIsolation:
    @pytest.mark.asyncio
    async def test_dev_observe_does_not_touch_preference_model(self):
        """10 dev-agent observations must leave PreferenceModel.action_stats empty."""
        twin = _make_twin_state()
        cmd = _make_cmd("write a function", source="voice")

        for _ in range(10):
            await twin._persist_observation(cmd, "WRITE_FILE code.py", namespace="dev_agent")

        # PreferenceModel should have no entries for WRITE_FILE
        assert "WRITE_FILE" not in twin._preference_model.action_stats

    @pytest.mark.asyncio
    async def test_dev_observe_does_not_call_semantic_memory_add(self):
        twin = _make_twin_state()
        cmd = _make_cmd("explain this code", source="voice")

        await twin._persist_observation(cmd, "EXPLAIN", namespace="dev_agent")

        twin._semantic_memory.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_dev_observe_does_not_increment_session_cmd_count(self):
        twin = _make_twin_state()
        cmd = _make_cmd("run tests", source="voice")
        before = twin._session_cmd_count

        for _ in range(5):
            await twin._persist_observation(cmd, "RUN_TERMINAL pytest", namespace="dev_agent")

        assert twin._session_cmd_count == before

    @pytest.mark.asyncio
    async def test_accessibility_observe_unchanged_after_dev_calls(self):
        """Accessibility session history must not accumulate dev-agent texts."""
        twin = _make_twin_state()

        # Make one accessibility observation first
        acc_cmd = _make_cmd("scroll down", source="tilt")
        await twin._persist_observation(acc_cmd, "SCROLL down", namespace="accessibility")

        # Then 10 dev calls
        dev_cmd = _make_cmd("write some code", source="voice")
        for _ in range(10):
            await twin._persist_observation(dev_cmd, "WRITE_FILE", namespace="dev_agent")

        # Accessibility slice should only contain the one accessibility text
        acc_history = twin.get_session_context("accessibility")
        assert all("scroll down" in h or h == "scroll down" for h in acc_history)
        assert "write some code" not in acc_history


# ---------------------------------------------------------------------------
# Dev namespace auto-clear
# ---------------------------------------------------------------------------

class TestDevNamespaceAutoClear:
    @pytest.mark.asyncio
    async def test_get_session_context_dev_agent_clears_on_read(self):
        twin = _make_twin_state()
        cmd = _make_cmd("explain error", source="voice")

        await twin._persist_observation(cmd, "EXPLAIN", namespace="dev_agent")
        assert len(twin._session_history["dev_agent"]) == 1

        # First read returns the data
        history = twin.get_session_context("dev_agent")
        assert len(history) == 1

        # Second read returns empty — was cleared
        history2 = twin.get_session_context("dev_agent")
        assert history2 == []

    def test_clear_dev_namespace_empties_list(self):
        twin = _make_twin_state()
        twin._session_history["dev_agent"] = ["cmd1", "cmd2", "cmd3"]

        twin.clear_dev_namespace()

        assert twin._session_history["dev_agent"] == []

    def test_clear_dev_namespace_does_not_touch_accessibility(self):
        twin = _make_twin_state()
        twin._session_history["accessibility"] = ["scroll", "click"]
        twin._session_history["dev_agent"] = ["plan", "execute"]

        twin.clear_dev_namespace()

        assert twin._session_history["accessibility"] == ["scroll", "click"]
        assert twin._session_history["dev_agent"] == []

    @pytest.mark.asyncio
    async def test_accessibility_get_session_context_does_not_auto_clear(self):
        """Accessibility history should NOT auto-clear on read."""
        twin = _make_twin_state()
        twin._session_history["accessibility"] = ["click", "scroll"]

        first = twin.get_session_context("accessibility")
        second = twin.get_session_context("accessibility")

        assert first == second == ["click", "scroll"]


# ---------------------------------------------------------------------------
# Dev namespace depth cap (max 5)
# ---------------------------------------------------------------------------

class TestDevNamespaceDepthCap:
    @pytest.mark.asyncio
    async def test_dev_history_capped_at_5_entries(self):
        twin = _make_twin_state()
        for i in range(10):
            cmd = _make_cmd(f"dev command {i}", source="voice")
            await twin._persist_observation(cmd, "EXPLAIN", namespace="dev_agent")

        assert len(twin._session_history["dev_agent"]) <= 5

    @pytest.mark.asyncio
    async def test_dev_history_retains_most_recent(self):
        twin = _make_twin_state()
        for i in range(8):
            cmd = _make_cmd(f"cmd {i}", source="voice")
            await twin._persist_observation(cmd, "EXPLAIN", namespace="dev_agent")

        history = twin._session_history["dev_agent"]
        # Should contain the last 5 entries
        assert f"cmd {7}" in history
        assert f"cmd {6}" in history


# ---------------------------------------------------------------------------
# Accessibility observations still accumulate normally
# ---------------------------------------------------------------------------

class TestAccessibilityNamespace:
    @pytest.mark.asyncio
    async def test_accessibility_observe_updates_preference_model(self):
        twin = _make_twin_state()
        cmd = _make_cmd("click here", source="voice")
        await twin._persist_observation(cmd, "CLICK button", namespace="accessibility")

        assert "CLICK" in twin._preference_model.action_stats

    @pytest.mark.asyncio
    async def test_accessibility_observe_calls_semantic_memory_add(self):
        twin = _make_twin_state()
        cmd = _make_cmd("scroll down", source="tilt")
        await twin._persist_observation(cmd, "SCROLL down", namespace="accessibility")

        twin._semantic_memory.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_accessibility_observe_increments_cmd_count(self):
        twin = _make_twin_state()
        cmd = _make_cmd("type hello", source="voice")
        before = twin._session_cmd_count

        await twin._persist_observation(cmd, "TYPE hello", namespace="accessibility")

        assert twin._session_cmd_count == before + 1

    @pytest.mark.asyncio
    async def test_accessibility_snapshot_session_context_uses_accessibility_slice(self):
        twin = _make_twin_state()
        twin._session_history["accessibility"] = ["scroll down", "click here"]
        twin._session_history["dev_agent"] = ["plan code", "execute test"]

        snapshot = twin._build_snapshot()

        assert "scroll down" in snapshot.session_context
        assert "plan code" not in snapshot.session_context
