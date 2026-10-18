from __future__ import annotations

import asyncio
import threading

import pytest

import core.command_executor as command_executor
from core.action_lifecycle import ActionIdentity, ActionStatus
from core.command_executor import Command, CommandExecutor
from core.execution_policy import ExecutionScope
from core.proc_utils import ProcessTimeoutError, TerminationEvidence


@pytest.mark.parametrize("late_failure", [False, True])
async def test_command_executor_timeout_retains_local_worker_and_original_identity(
    monkeypatch, late_failure
):
    executor = CommandExecutor()
    release = threading.Event()
    monkeypatch.setattr(executor, "_tool_timeout_ms", lambda action: asyncio.sleep(0, result=10))
    monkeypatch.setattr(command_executor, "_RECONCILIATION_GRACE_S", 0.01)
    monkeypatch.setattr(command_executor, "_get_action_verifier", lambda: None)

    def dispatch(action, cmd):
        release.wait(2)
        if late_failure:
            raise RuntimeError("late failure")
        return {"late": "success"}

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    cmd = Command(
        text="x",
        action="WRITE_FILE",
        source="test",
        trace_id="trace-local",
        action_id="local-" + "5" * 40,
        run_id=9,
        step_num=6,
    )

    response = await executor.execute(cmd)
    assert response["status"] == "outcome_unknown"
    assert response["trace_id"] == "trace-local"
    release.set()
    await asyncio.sleep(0.03)

    observed = await executor._action_registry.query(
        ActionIdentity(cmd.action_id, "WRITE_FILE", "trace-local", 9, 6)
    )
    assert observed is not None
    assert observed.status is (
        ActionStatus.FAILED if late_failure else ActionStatus.COMPLETED
    )
    assert observed.trace_id == "trace-local"


@pytest.mark.parametrize(
    ("verified", "expected"),
    [(True, "error"), (False, "outcome_unknown")],
)
async def test_command_terminal_timeout_uses_process_kill_evidence(
    monkeypatch, verified, expected
):
    executor = CommandExecutor()
    monkeypatch.setattr(executor, "_tool_timeout_ms", lambda action: asyncio.sleep(0, result=1000))
    monkeypatch.setattr(command_executor, "_get_action_verifier", lambda: None)

    def dispatch(action, cmd):
        raise ProcessTimeoutError(
            "cmd", 1, termination=TerminationEvidence(verified, "test")
        )

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    result = await executor.execute(Command(
        text="cmd", action="RUN_TERMINAL", source="test",
        action_id="terminal-" + "6" * 40,
    ))
    assert result["status"] == expected


async def test_command_write_uses_atomic_replacement(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "value.txt"
    target.write_text("old", encoding="utf-8")
    executor = CommandExecutor(ExecutionScope(root, [root]))
    monkeypatch.setattr(command_executor, "_get_action_verifier", lambda: None)
    calls = []

    def atomic(path, content):
        calls.append((path, content))
        path.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    monkeypatch.setattr(command_executor, "atomic_write_text", atomic)
    result = await executor.execute(Command(
        text="new", action="WRITE_FILE", source="test",
        params={"path": str(target), "content": "new"},
        action_id="atomic-" + "7" * 40,
    ))
    assert result["status"] == "ok"
    assert calls == [(target.resolve(), "new")]


async def test_late_file_write_reconciles_by_expected_hash(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "late.txt"
    executor = CommandExecutor(ExecutionScope(root, [root]))
    release = threading.Event()
    monkeypatch.setattr(executor, "_tool_timeout_ms", lambda action: asyncio.sleep(0, result=10))
    monkeypatch.setattr(command_executor, "_RECONCILIATION_GRACE_S", 0.01)
    monkeypatch.setattr(command_executor, "_get_action_verifier", lambda: None)

    def dispatch(action, cmd):
        release.wait(2)
        target.write_text("intended", encoding="utf-8")
        return {"written": str(target)}

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    cmd = Command(
        text="intended", action="WRITE_FILE", source="test",
        params={"path": str(target), "content": "intended"},
        action_id="hash-" + "8" * 40, trace_id="hash-trace",
    )
    first = await executor.execute(cmd)
    assert first["status"] == "outcome_unknown"
    release.set()
    await asyncio.sleep(0.03)
    observed = await executor._action_registry.query(
        ActionIdentity(cmd.action_id, "WRITE_FILE", "hash-trace", None, None)
    )
    assert observed is not None
    assert observed.reconciliation == "file evidence: intended hash present"
