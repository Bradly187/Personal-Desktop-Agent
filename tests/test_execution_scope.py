"""Phase 1 execution-scope canonicalization, sharing, and command tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.command_executor import (
    Command, CommandExecutor, _mountinfo_proves_drvfs,
    _path_for_windows_proxy, _translate_wsl_path_for_windows_proxy,
)
from core.execution_policy import ExecutionScope


def _scope(tmp_path: Path) -> tuple[ExecutionScope, Path, Path]:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    return ExecutionScope(allowed, [allowed]), allowed, outside


@pytest.mark.parametrize("value", ["", "   ", "bad\x00path", "C:relative.txt", "C:", None, {}])
def test_uncertain_paths_fail_closed(tmp_path, value):
    scope, _, _ = _scope(tmp_path)
    decision = scope.authorize_path(value, operation="WRITE_FILE")
    assert decision.allowed is False
    assert decision.canonical_path is None
    assert decision.reason


def test_relative_path_resolves_against_repository_root(tmp_path):
    scope, allowed, _ = _scope(tmp_path)
    decision = scope.authorize_path("nested/new.txt", operation="WRITE_FILE")
    assert decision.allowed is True
    assert decision.canonical_path == allowed / "nested" / "new.txt"


def test_absolute_and_traversal_escapes_are_denied(tmp_path):
    scope, allowed, outside = _scope(tmp_path)
    absolute = scope.authorize_path(str(outside / "escape.txt"), operation="WRITE_FILE")
    traversal = scope.authorize_path("../outside/escape.txt", operation="WRITE_FILE")
    assert absolute.allowed is False
    assert traversal.allowed is False
    assert absolute.canonical_path == outside / "escape.txt"
    assert traversal.canonical_path == outside / "escape.txt"
    assert allowed != outside


def test_symlink_escape_is_denied(tmp_path):
    scope, allowed, outside = _scope(tmp_path)
    link = allowed / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    decision = scope.authorize_path(str(link / "escape.txt"), operation="WRITE_FILE")
    assert decision.allowed is False
    assert decision.canonical_path == outside / "escape.txt"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_junction_escape_is_denied_on_windows(tmp_path):
    scope, allowed, outside = _scope(tmp_path)
    junction = allowed / "junction"
    made = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"junction creation unavailable: {made.stderr or made.stdout}")
    try:
        decision = scope.authorize_path(
            str(junction / "escape.txt"), operation="WRITE_FILE"
        )
        assert decision.allowed is False
        assert decision.canonical_path == outside / "escape.txt"
    finally:
        os.rmdir(junction)


async def test_command_executor_preserves_direct_and_public_response_shapes(tmp_path, monkeypatch):
    scope, allowed, outside = _scope(tmp_path)
    executor = CommandExecutor(scope)
    denied = Command(
        text="x", action="WRITE_FILE", source="test",
        params={"path": str(outside / "x.txt"), "content": "x"},
    )
    direct = executor._dispatch("WRITE_FILE", denied)
    assert direct == {
        "status": "error",
        "error": f"WRITE_FILE denied: {outside / 'x.txt'} is outside the writable-root allowlist",
    }

    monkeypatch.setattr("core.command_executor._get_action_verifier", lambda: None)
    result = await executor.execute(denied)
    assert result["status"] == "ok"
    assert result["action"] == "WRITE_FILE"
    assert result["result"] == direct
    assert not (outside / "x.txt").exists()
    assert allowed.exists()


async def test_proxy_preflight_denies_escape_and_forwards_canonical_cwd(tmp_path, monkeypatch):
    scope, allowed, outside = _scope(tmp_path)
    assert scope.set_active_root(str(allowed), human_confirmed=False).allowed
    executor = CommandExecutor(scope)
    proxy = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(executor, "_proxy_execute", proxy)
    monkeypatch.setattr("core.command_executor._PROXY_URL", "http://proxy.test")

    denied = Command(
        text="x", action="WRITE_FILE", source="test",
        params={"path": str(outside / "x.txt"), "content": "x"},
    )
    result = await executor.execute(denied)
    assert result["status"] == "ok"
    assert result["action"] == "WRITE_FILE"
    assert result["result"]["status"] == "error"
    proxy.assert_not_awaited()

    denied_terminal = Command(
        text="echo no", action="RUN_TERMINAL", source="test",
        params={"command": "echo no", "cwd": str(outside)},
    )
    terminal_denial = await executor.execute(denied_terminal)
    assert terminal_denial["status"] == "ok"
    assert terminal_denial["action"] == "RUN_TERMINAL"
    assert terminal_denial["result"]["status"] == "error"
    proxy.assert_not_awaited()

    terminal = Command(
        text="echo ok", action="RUN_TERMINAL", source="test",
        params={"command": "echo ok"},
    )
    await executor.execute(terminal)
    forwarded = proxy.await_args.args[0]
    assert forwarded.params["cwd"] == str(allowed)


@pytest.mark.skipif(os.name != "nt", reason="Windows proxy receiver semantics")
def test_wsl_drvfs_paths_translate_into_the_windows_proxy_scope():
    from core.command_executor import _REPO_ROOT

    drive = _REPO_ROOT.drive[0]
    rel_parts = _REPO_ROOT.parts[1:]
    wsl_target = PurePosixPath(
        f"/mnt/{drive.lower()}", *rel_parts, "proxy-boundary.txt"
    )
    translated = _translate_wsl_path_for_windows_proxy(wsl_target)
    assert translated == "\\".join(
        [f"{drive.upper()}:", *rel_parts, "proxy-boundary.txt"]
    )

    receiver_scope = ExecutionScope()
    received = receiver_scope.authorize_path(
        translated, operation="WRITE_FILE"
    )
    assert received.allowed is True
    assert received.canonical_path == _REPO_ROOT / "proxy-boundary.txt"


def test_one_scope_is_shared_across_runtime_components(tmp_path):
    from core.hybrid_coordinator import HybridCoordinator
    from core.ipad_bridge import IPadBridge
    from inference.dev_agent import DevAgent

    scope, _, _ = _scope(tmp_path)
    coordinator = HybridCoordinator(execution_scope=scope)
    dev_agent = DevAgent(
        router=MagicMock(), coordinator=coordinator, execution_scope=scope
    )
    coordinator.set_dev_agent(dev_agent)
    bridge = IPadBridge(token="test-token", execution_scope=scope)

    assert coordinator._execution_scope is scope
    assert coordinator._executor._execution_scope is scope
    assert dev_agent._execution_scope is scope
    assert dev_agent._saga_manager._execution_scope is scope
    assert dev_agent._tester is not None
    assert dev_agent._tester._execution_scope is scope
    assert bridge._execution_scope is scope
    assert bridge._executor._execution_scope is scope


def test_confirmed_dynamic_root_is_immediately_shared(tmp_path):
    from core.hybrid_coordinator import HybridCoordinator
    from inference.dev_agent import DevAgent

    scope, allowed, outside = _scope(tmp_path)
    project = outside / "project"
    project.mkdir()
    coordinator = HybridCoordinator(execution_scope=scope)
    dev_agent = DevAgent(
        router=MagicMock(), coordinator=coordinator, execution_scope=scope
    )
    coordinator.set_dev_agent(dev_agent)

    first = coordinator.set_active_directory(str(project))
    assert first == {"status": "confirm_required", "path": str(project)}
    activated = coordinator.set_active_directory(str(project), confirm=True)
    assert activated["status"] == "activated"
    assert coordinator._executor._execution_scope is dev_agent._execution_scope
    assert dev_agent._execution_scope.authorize_path(
        "new.py", operation="WRITE_FILE"
    ).canonical_path == project / "new.py"

    cmd = Command(
        text="ok", action="WRITE_FILE", source="test",
        params={"path": "new.py", "content": "ok"},
    )
    result = coordinator._executor._dispatch("WRITE_FILE", cmd)
    assert result["written"] == str(project / "new.py")
    assert (project / "new.py").read_text(encoding="utf-8") == "ok"
    assert allowed.exists()


def test_wsl_proxy_translation_requires_proven_drive_mount():
    path = PurePosixPath("/mnt/e/project/file.txt")
    assert _translate_wsl_path_for_windows_proxy(path) == "E:\\project\\file.txt"
    assert _path_for_windows_proxy(
        path, platform="posix", mount_verifier=lambda candidate: True
    ) == "E:\\project\\file.txt"
    assert _path_for_windows_proxy(
        path, platform="posix", mount_verifier=lambda candidate: False
    ) is None


@pytest.mark.parametrize("mountinfo,expected", [
    (
        "36 25 0:32 / /mnt/e rw,relatime - drvfs E: rw",
        True,
    ),
    (
        "36 25 0:32 / /mnt/e rw,relatime - 9p drvfs rw,aname=drvfs;path=E:\\",
        True,
    ),
    (
        "36 25 0:32 / /mnt/e rw,relatime - 9p drvfs rw,aname=drvfs;path=E:\\private",
        False,
    ),
    (
        "36 25 0:32 / /mnt/e rw,relatime - 9p drvfs rw,aname=drvfs;path=F:\\",
        False,
    ),
    (
        "36 25 0:32 / /mnt/e rw,relatime - ext4 E: rw",
        False,
    ),
    (
        "36 25 0:32 / /mnt/f rw,relatime - drvfs E: rw",
        False,
    ),
])
def test_mountinfo_drvfs_proof(mountinfo, expected):
    assert _mountinfo_proves_drvfs(
        PurePosixPath("/mnt/e/project/file.txt"), mountinfo
    ) is expected


async def test_proxy_preflight_runs_off_event_loop(tmp_path, monkeypatch):
    import threading

    scope, _, _ = _scope(tmp_path)
    executor = CommandExecutor(scope)
    loop_thread = threading.get_ident()
    worker_threads = []

    def preflight(cmd):
        worker_threads.append(threading.get_ident())
        return None, None, {"status": "error", "error": "denied"}

    monkeypatch.setattr(executor, "_proxy_scope_preflight", preflight)
    monkeypatch.setattr("core.command_executor._PROXY_URL", "http://proxy.test")
    result = await executor.execute(Command(
        text="x", action="WRITE_FILE", source="test", params={"path": "x"},
    ))

    assert result["result"]["error"] == "denied"
    assert worker_threads and worker_threads[0] != loop_thread
