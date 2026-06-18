"""Tests for FINDING 5 — silent audit trail gaps.

Verifies that:
  1. CommandExecutor.execute() writes 'command_execution_failed' to the audit log
     on TimeoutError and uncaught Exception.
  2. approval_hook._vibe_summary() logs at WARNING level when Ollama is unavailable.
  3. CommandExecutor functions correctly without an audit log wired (no crash).

Run:
    python -m pytest tests/test_audit_trail_coverage.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command, CommandExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmd(action: str = "CLICK", text: str = "click save") -> Command:
    return Command(text=text, action=action, source="voice", trace_id="trace-001")


@pytest.fixture
def mock_audit():
    """A MagicMock that mimics AuditLog with async .log()."""
    audit = MagicMock()
    audit.available = True
    audit.log = AsyncMock(return_value=1)
    return audit


@pytest.fixture
def executor(mock_audit) -> CommandExecutor:
    ex = CommandExecutor()
    ex.set_audit_log(mock_audit)
    return ex


def _patch_executor_for_dispatch_error(monkeypatch, executor, exc_factory, timeout_ms=5000):
    """Helper: patch asyncio.to_thread to raise exc_factory() and _tool_timeout_ms to timeout_ms."""
    async def _fake_to_thread(fn, *a, **kw):
        raise exc_factory()
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    async def _fixed_timeout(action):
        return timeout_ms
    monkeypatch.setattr(executor, "_tool_timeout_ms", _fixed_timeout)

    monkeypatch.setattr("core.command_executor._get_action_verifier", lambda: None)


# ---------------------------------------------------------------------------
# CommandExecutor audit on TimeoutError
# ---------------------------------------------------------------------------

async def test_timeout_writes_audit_event(executor, mock_audit, monkeypatch):
    """TimeoutError inside execute() writes a command_execution_failed audit event."""
    _patch_executor_for_dispatch_error(monkeypatch, executor, TimeoutError, timeout_ms=5000)

    result = await executor.execute(_cmd("CLICK"))

    assert result["status"] == "error"
    assert "timeout" in result["error"]

    mock_audit.log.assert_called_once()
    kwargs = mock_audit.log.call_args[1]
    assert kwargs["outcome"] == "timeout"
    assert kwargs.get("tool") == "CLICK"
    assert "command_execution_failed" in mock_audit.log.call_args[0]


async def test_timeout_audit_includes_trace_id(executor, mock_audit, monkeypatch):
    """The audit event for a timeout includes the command trace_id in params."""
    _patch_executor_for_dispatch_error(monkeypatch, executor, TimeoutError)

    await executor.execute(_cmd("CLICK"))

    kwargs = mock_audit.log.call_args[1]
    params = kwargs.get("params") or {}
    assert params.get("trace_id") == "trace-001"


# ---------------------------------------------------------------------------
# CommandExecutor audit on generic Exception
# ---------------------------------------------------------------------------

async def test_exception_writes_audit_event(executor, mock_audit, monkeypatch):
    """An uncaught Exception in _dispatch() writes a command_execution_failed audit event."""
    _patch_executor_for_dispatch_error(
        monkeypatch, executor, lambda: RuntimeError("pyautogui unavailable")
    )

    result = await executor.execute(_cmd("CLICK"))

    assert result["status"] == "error"
    assert "pyautogui" in result["error"]

    mock_audit.log.assert_called_once()
    kwargs = mock_audit.log.call_args[1]
    assert kwargs["outcome"] == "exception"
    assert "pyautogui" in (kwargs.get("detail") or "")


async def test_exception_audit_severity_is_warning(executor, mock_audit, monkeypatch):
    """command_execution_failed events are logged at WARNING severity."""
    _patch_executor_for_dispatch_error(
        monkeypatch, executor, lambda: RuntimeError("boom")
    )

    await executor.execute(_cmd("CLICK"))

    kwargs = mock_audit.log.call_args[1]
    assert kwargs.get("severity") == "warning"


# ---------------------------------------------------------------------------
# Graceful degradation — no audit log wired
# ---------------------------------------------------------------------------

async def test_no_crash_without_audit_log(monkeypatch):
    """execute() returns an error dict even when no audit log is wired."""
    ex = CommandExecutor()

    async def _fake_to_thread(fn, *a, **kw):
        raise RuntimeError("no audit wired")
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    async def _default_timeout(action):
        return 5000
    monkeypatch.setattr(ex, "_tool_timeout_ms", _default_timeout)
    monkeypatch.setattr("core.command_executor._get_action_verifier", lambda: None)

    result = await ex.execute(_cmd("CLICK"))
    assert result["status"] == "error"   # no crash


async def test_no_crash_when_audit_unavailable(monkeypatch):
    """If audit_log.available is False, execute() skips the write and returns normally."""
    ex = CommandExecutor()
    unavail_audit = MagicMock()
    unavail_audit.available = False
    unavail_audit.log = AsyncMock(side_effect=AssertionError("should not be called"))
    ex.set_audit_log(unavail_audit)

    async def _fake_to_thread(fn, *a, **kw):
        raise RuntimeError("test error")
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    async def _default_timeout(action):
        return 5000
    monkeypatch.setattr(ex, "_tool_timeout_ms", _default_timeout)
    monkeypatch.setattr("core.command_executor._get_action_verifier", lambda: None)

    result = await ex.execute(_cmd("CLICK"))
    assert result["status"] == "error"
    unavail_audit.log.assert_not_called()


# ---------------------------------------------------------------------------
# set_audit_log wiring
# ---------------------------------------------------------------------------

async def test_set_audit_log_stores_reference():
    """set_audit_log() stores the reference; None by default."""
    ex = CommandExecutor()
    assert ex._audit_log is None

    mock = MagicMock()
    mock.available = True
    ex.set_audit_log(mock)
    assert ex._audit_log is mock


# ---------------------------------------------------------------------------
# approval_hook vibe_summary fallback logging
# ---------------------------------------------------------------------------

async def test_vibe_summary_unavailable_logs_warning(monkeypatch, caplog):
    """When Ollama is unreachable, _vibe_summary logs at WARNING level."""
    import logging
    import approval_hook

    config = {"vibe_diff_tools": ["Bash"], "vibe_diff_model": "llama3.1:8b"}

    def _connection_refused(req, timeout=None):
        raise ConnectionRefusedError("ollama down")

    monkeypatch.setattr(urllib.request, "urlopen", _connection_refused)
    # Suppress the async audit write so it doesn't try to open a real DB
    monkeypatch.setattr(approval_hook, "_try_audit_vibe_unavailable", lambda exc: None)

    with caplog.at_level(logging.WARNING):
        result = approval_hook._vibe_summary(
            "Bash", {"command": "rm -rf /tmp/test"}, config
        )

    assert result is None  # Falls back to static prompt, not a hard error
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("vibe summary" in m.lower() or "vibe" in m.lower() for m in warning_msgs), \
        f"Expected a WARNING about vibe summary, got: {warning_msgs}"


async def test_vibe_summary_none_when_not_in_vibe_tools(monkeypatch):
    """_vibe_summary returns None immediately when tool not in vibe_diff_tools (no Ollama call)."""
    import approval_hook

    config = {"vibe_diff_tools": ["Write"], "vibe_diff_model": "llama3.1:8b"}

    def _should_not_be_called(*_a, **_kw):
        raise AssertionError("urlopen should not be called for non-vibe tools")

    monkeypatch.setattr(urllib.request, "urlopen", _should_not_be_called)

    result = approval_hook._vibe_summary("Bash", {"command": "echo hi"}, config)
    assert result is None
