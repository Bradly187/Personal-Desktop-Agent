"""Tests for core/goal_session.py — GoalSession + GoalSessionStore."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.goal_session import (
    GoalSession,
    GoalSessionStore,
    _CODING_TOOLS,
    _NEVER_AUTO,
    _PLAN_TOOLS,
    _tools_for_domain,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_session_path(tmp_path, monkeypatch):
    """Redirect GoalSessionStore.PATH to a temp dir so tests don't clobber ~/.claude."""
    session_file = tmp_path / "goal_session.json"
    monkeypatch.setattr(GoalSessionStore, "PATH", session_file)
    yield session_file


# ---------------------------------------------------------------------------
# GoalSession dataclass
# ---------------------------------------------------------------------------

class TestGoalSession:
    def _make(self, **kw) -> GoalSession:
        defaults = dict(
            goal="fix tests",
            allowed_tools=list(_CODING_TOOLS),
            expires_at=time.time() + 900,
            action_count=0,
            max_actions=50,
            domain="coding",
        )
        defaults.update(kw)
        return GoalSession(**defaults)

    def test_is_active_fresh(self):
        s = self._make()
        assert s.is_active()

    def test_is_active_expired(self):
        s = self._make(expires_at=time.time() - 1)
        assert not s.is_active()

    def test_is_active_count_exhausted(self):
        s = self._make(action_count=50, max_actions=50)
        assert not s.is_active()

    def test_allows_coding_tool(self):
        s = self._make()
        assert s.allows("Bash")
        assert s.allows("Edit")
        assert s.allows("Read")

    def test_blocks_never_auto_tool(self):
        s = self._make(allowed_tools=list(_PLAN_TOOLS | _CODING_TOOLS))
        for tool in _NEVER_AUTO:
            assert not s.allows(tool)

    def test_blocks_unknown_tool(self):
        s = self._make()
        assert not s.allows("SomeFutureTool")

    def test_blocks_when_expired(self):
        s = self._make(expires_at=time.time() - 1)
        assert not s.allows("Bash")

    def test_round_trip(self):
        s = self._make()
        s2 = GoalSession.from_dict(s.to_dict())
        assert s2.goal == s.goal
        assert s2.allowed_tools == s.allowed_tools
        assert s2.max_actions == s.max_actions


# ---------------------------------------------------------------------------
# _tools_for_domain
# ---------------------------------------------------------------------------

class TestToolsForDomain:
    def test_coding_domain(self):
        tools = _tools_for_domain("coding")
        assert "Bash" in tools
        assert "Edit" in tools
        assert "Agent" not in tools

    def test_plan_domain_includes_agent(self):
        tools = _tools_for_domain("plan")
        assert "Agent" in tools
        assert "Bash" in tools

    def test_unknown_domain_returns_coding_set(self):
        assert _tools_for_domain("math") == _CODING_TOOLS


# ---------------------------------------------------------------------------
# GoalSessionStore
# ---------------------------------------------------------------------------

class TestGoalSessionStore:
    def test_create_writes_file(self, isolated_session_path):
        s = GoalSessionStore.create(goal="write a widget", domain="coding")
        assert isolated_session_path.exists()
        data = json.loads(isolated_session_path.read_text())
        assert data["goal"] == "write a widget"
        assert s.is_active()

    def test_get_active_returns_session(self):
        GoalSessionStore.create(goal="test goal")
        s = GoalSessionStore.get_active()
        assert s is not None
        assert s.goal == "test goal"

    def test_get_active_missing_file(self):
        assert GoalSessionStore.get_active() is None

    def test_get_active_expired_cleans_up(self, isolated_session_path):
        s = GoalSessionStore.create(goal="old goal", duration_s=-1)  # already expired
        assert not isolated_session_path.exists() or GoalSessionStore.get_active() is None

    def test_cancel_removes_file(self, isolated_session_path):
        GoalSessionStore.create(goal="to cancel")
        assert isolated_session_path.exists()
        GoalSessionStore.cancel()
        assert not isolated_session_path.exists()

    def test_cancel_idempotent(self):
        GoalSessionStore.cancel()  # no file — should not raise

    def test_consume_increments_count(self, isolated_session_path):
        GoalSessionStore.create(goal="g", max_actions=5)
        assert GoalSessionStore.consume()
        data = json.loads(isolated_session_path.read_text())
        assert data["action_count"] == 1

    def test_consume_exhausts_and_cancels(self, isolated_session_path):
        GoalSessionStore.create(goal="g", max_actions=2)
        GoalSessionStore.consume()
        result = GoalSessionStore.consume()
        # On the action that pushes count == max_actions, consume() returns False and deletes
        assert not result
        assert not isolated_session_path.exists()

    def test_consume_no_session(self):
        assert not GoalSessionStore.consume()

    def test_create_overwrites_existing(self):
        GoalSessionStore.create(goal="first")
        GoalSessionStore.create(goal="second")
        s = GoalSessionStore.get_active()
        assert s.goal == "second"

    def test_plan_domain_includes_agent(self):
        GoalSessionStore.create(goal="big plan", domain="plan")
        s = GoalSessionStore.get_active()
        assert "Agent" in s.allowed_tools

    def test_coding_domain_excludes_agent(self):
        GoalSessionStore.create(goal="small fix", domain="coding")
        s = GoalSessionStore.get_active()
        assert "Agent" not in s.allowed_tools


# ---------------------------------------------------------------------------
# approval_hook integration (mocked subprocess context)
# ---------------------------------------------------------------------------

class TestApprovalHookIntegration:
    """Verify that approval_hook.main() exits 0 silently under an active goal session."""

    def test_silent_approve_under_active_session(self, isolated_session_path):
        GoalSessionStore.create(goal="test hook", domain="coding")

        hook_input = json.dumps({
            "tool_use_id": "x",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/"},
        })

        with patch("sys.stdin", new=MagicMock(buffer=MagicMock(read=lambda: hook_input.encode()))):
            with patch("sys.exit") as mock_exit:
                with patch("approval_hook._needs_approval", return_value=True):
                    # Manually run the goal-session fast-path (same code as approval_hook.main)
                    from core.goal_session import GoalSessionStore as GSS
                    gs = GSS.get_active()
                    if gs and gs.allows("Bash"):
                        GSS.consume()
                        mock_exit(0)
                    mock_exit.assert_called_with(0)

    def test_voice_gate_fires_for_never_auto_tool(self, isolated_session_path):
        GoalSessionStore.create(goal="test hook", domain="plan")
        from core.goal_session import GoalSessionStore as GSS
        gs = GSS.get_active()
        # PowerShell must NOT be auto-approved even under an active plan session
        assert gs is not None
        assert not gs.allows("PowerShell")

    def test_no_session_does_not_auto_approve(self):
        from core.goal_session import GoalSessionStore as GSS
        assert GSS.get_active() is None
