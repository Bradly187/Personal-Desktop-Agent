"""Phase 1 StepExecutor scope checks and replan chokepoint tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import inference.executors.plan_executor as plan_executor
import inference.step_executor as step_executor
from core.execution_policy import ExecutionScope, ScopeDecision
from inference.dev_agent import AgentStep, DevAgent


def _agent(tmp_path: Path) -> tuple[DevAgent, Path, Path]:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    scope = ExecutionScope(allowed, [allowed])
    router = MagicMock()
    router.edit_format_for.return_value = "whole_file"
    agent = DevAgent(router=router, execution_scope=scope)
    agent._plan_authorized = True
    agent._critic = None
    agent._critic_enabled = False
    return agent, allowed, outside


@pytest.mark.parametrize("action", ["WRITE_FILE", "EDIT_FILE"])
@pytest.mark.parametrize("escape_kind", ["absolute", "traversal"])
async def test_direct_file_escape_denied_before_any_read_or_side_effect(
    tmp_path, action, escape_kind, monkeypatch
):
    agent, _, outside = _agent(tmp_path)
    target = str(outside / "escape.py") if escape_kind == "absolute" else "../outside/escape.py"
    step = AgentStep(action=action, args=target, body="print('unsafe')")

    apply = MagicMock()
    diff = MagicMock()
    write = MagicMock()
    snapshot = MagicMock()
    confirm = AsyncMock(return_value=True)
    monkeypatch.setattr(step_executor, "apply_edit", apply)
    monkeypatch.setattr(step_executor, "diff_for_confirm", diff)
    monkeypatch.setattr(step_executor, "write_file", write)
    monkeypatch.setattr(step_executor, "confirm_destructive_op", confirm)
    agent._saga_manager.snapshot_for_write = snapshot

    with patch.object(step_executor.Path, "mkdir") as mkdir:
        with pytest.raises(PermissionError, match="outside"):
            await step_executor.execute_step(agent, step)

    confirm.assert_not_awaited()
    apply.assert_not_called()
    diff.assert_not_called()
    snapshot.assert_not_called()
    mkdir.assert_not_called()
    write.assert_not_called()
    assert not (outside / "escape.py").exists()


async def test_step_executor_uses_policy_canonical_path_everywhere(tmp_path, monkeypatch):
    agent, allowed, _ = _agent(tmp_path)
    step = AgentStep(action="WRITE_FILE", args="nested/new.py", body="content")
    canonical = str(allowed / "nested" / "new.py")

    apply = MagicMock(return_value="resolved")
    write = MagicMock(return_value="Written")
    snapshot = MagicMock(return_value={
        "path": canonical, "existed": False, "backup": None,
    })
    monkeypatch.setattr(step_executor, "apply_edit", apply)
    monkeypatch.setattr(step_executor, "write_file", write)
    monkeypatch.setattr(
        step_executor, "confirm_destructive_op", AsyncMock(return_value=True)
    )
    agent._saga_manager.snapshot_for_write = snapshot
    agent._maybe_run_tester = AsyncMock(return_value="Written")

    result = await step_executor.execute_step(agent, step)

    assert result == "Written"
    assert step.args == canonical
    assert apply.call_args.args[0] == canonical
    snapshot.assert_called_once_with(canonical)
    write.assert_called_once_with(canonical, "resolved")


async def test_critic_path_reauthorizes_at_saga_snapshot(tmp_path, monkeypatch):
    agent, allowed, _ = _agent(tmp_path)
    target = allowed / "reviewed.py"
    step = AgentStep(action="WRITE_FILE", args=str(target), body="content")
    agent._critic = MagicMock()
    agent._critic_enabled = True
    agent._critic_review = AsyncMock(return_value=MagicMock(
        decision="pass", escalate=False,
    ))
    monkeypatch.setattr(step_executor, "apply_edit", MagicMock(return_value="resolved"))
    monkeypatch.setattr(step_executor, "diff_for_confirm", MagicMock(return_value="diff"))
    monkeypatch.setattr(
        step_executor, "confirm_destructive_op", AsyncMock(return_value=True)
    )
    write = MagicMock()
    monkeypatch.setattr(step_executor, "write_file", write)
    agent._execution_scope.authorize_path = MagicMock(side_effect=[
        ScopeDecision(True, target, allowed, None),
        ScopeDecision(False, target, None, "saga snapshot path is outside scope"),
    ])

    with pytest.raises(PermissionError, match="outside"):
        await step_executor.execute_step(agent, step)

    assert agent._execution_scope.authorize_path.call_count == 2
    write.assert_not_called()


async def test_terminal_uses_active_root_as_authorized_default_cwd(tmp_path, monkeypatch):
    agent, allowed, _ = _agent(tmp_path)
    active = allowed / "project"
    active.mkdir()
    assert agent._execution_scope.set_active_root(
        str(active), human_confirmed=False
    ).allowed
    run = MagicMock(return_value="ok")
    monkeypatch.setattr(step_executor, "run_terminal", run)
    monkeypatch.setattr(
        step_executor, "confirm_destructive_op", AsyncMock(return_value=True)
    )

    result = await step_executor.execute_step(
        agent, AgentStep(action="RUN_TERMINAL", args="echo ok")
    )

    assert result == "ok"
    run.assert_called_once_with(
        "echo ok",
        cwd=str(active),
        work_trust=agent._active_work_trust,
        containment_policy=agent._containment_policy,
    )


async def test_replan_injected_escape_hits_same_step_scope_chokepoint(
    tmp_path, monkeypatch
):
    agent, allowed, outside = _agent(tmp_path)
    agent._approved_verbs = frozenset({"WRITE_FILE"})
    agent._plan_authorized = True
    injected = AgentStep(
        action="WRITE_FILE", args=str(outside / "replan.py"), body="unsafe"
    )
    monkeypatch.setattr(
        plan_executor, "_replan", AsyncMock(return_value=[injected])
    )
    apply = MagicMock()
    write = MagicMock()
    monkeypatch.setattr(step_executor, "apply_edit", apply)
    monkeypatch.setattr(step_executor, "write_file", write)

    replanned = await plan_executor._try_replan(
        agent,
        "goal",
        [AgentStep(action="WRITE_FILE", args=str(allowed / "failed.py"))],
        [],
    )

    assert replanned == [injected]
    assert agent._plan_authorized is True  # same verb, so verb-only gate stays open
    with pytest.raises(PermissionError, match="outside"):
        await agent._execute_step(replanned[0])
    apply.assert_not_called()
    write.assert_not_called()
    assert not (outside / "replan.py").exists()


_GIT_CASES = [
    ("GIT_STATUS", "", "", "git_status", ()),
    ("GIT_DIFF", "--staged", "", "git_diff", ("--staged",)),
    ("GIT_COMMIT", "scope commit", "", "git_commit", ("scope commit",)),
    ("GIT_CHECKOUT", "-b scope-test", "", "git_checkout", ("-b scope-test",)),
    ("GITHUB_PR", "Scope PR", "Details", "github_pr", ("Scope PR", "Details")),
]


@pytest.mark.parametrize("action,args,body,helper_name,expected_args", _GIT_CASES)
async def test_git_actions_deny_unauthorized_default_cwd_before_execution(
    tmp_path, monkeypatch, action, args, body, helper_name, expected_args
):
    agent, _, _ = _agent(tmp_path)
    agent._execution_scope.authorize_cwd = MagicMock(return_value=ScopeDecision(
        False, None, None, "git cwd is outside scope",
    ))
    helper = MagicMock(return_value="should not run")
    confirm = AsyncMock(return_value=True)
    monkeypatch.setattr(step_executor, helper_name, helper)
    monkeypatch.setattr(step_executor, "confirm_destructive_op", confirm)

    with pytest.raises(PermissionError, match="outside scope"):
        await step_executor.execute_step(
            agent, AgentStep(action=action, args=args, body=body)
        )

    helper.assert_not_called()
    confirm.assert_not_awaited()


@pytest.mark.parametrize("action,args,body,helper_name,expected_args", _GIT_CASES)
async def test_git_actions_use_authorized_active_root(
    tmp_path, monkeypatch, action, args, body, helper_name, expected_args
):
    agent, allowed, _ = _agent(tmp_path)
    active = allowed / "git-project"
    active.mkdir()
    assert agent._execution_scope.set_active_root(
        str(active), human_confirmed=False
    ).allowed
    helper = MagicMock(return_value="ok")
    monkeypatch.setattr(step_executor, helper_name, helper)
    monkeypatch.setattr(
        step_executor, "confirm_destructive_op", AsyncMock(return_value=True)
    )

    result = await step_executor.execute_step(
        agent, AgentStep(action=action, args=args, body=body)
    )

    assert result == "ok"
    helper.assert_called_once_with(*expected_args, cwd=str(active))


def test_git_helpers_pass_explicit_cwd_and_sanitized_env_to_every_process(
    tmp_path, monkeypatch
):
    from inference.executors import git_executor

    calls = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setenv("WSL_ACTION_PROXY_TOKEN", "secret-token")
    monkeypatch.setenv("WSL_ACTION_PROXY_INSTANCE", "instance-id")
    monkeypatch.setenv("GH_TOKEN", "github-token")
    monkeypatch.setattr(git_executor.subprocess, "run", run)
    cwd = str(tmp_path)
    git_executor.git_status(cwd=cwd)
    git_executor.git_diff("--staged", cwd=cwd)
    git_executor.git_commit("message", cwd=cwd)
    git_executor.git_checkout("-b branch", cwd=cwd)
    git_executor.github_pr("title", "body", cwd=cwd)

    assert len(calls) == 6
    assert all(kwargs["cwd"] == cwd for _, kwargs in calls)
    assert all("WSL_ACTION_PROXY_TOKEN" not in kwargs["env"] for _, kwargs in calls)
    assert all("WSL_ACTION_PROXY_INSTANCE" not in kwargs["env"] for _, kwargs in calls)
    assert all(kwargs["env"]["GH_TOKEN"] == "github-token" for _, kwargs in calls)
    assert all(kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0" for _, kwargs in calls)


@pytest.mark.parametrize("action,args", [
    ("WRITE_FILE", "blocked.py"),
    ("RUN_TERMINAL", "echo blocked"),
    ("GIT_STATUS", ""),
])
async def test_initial_scope_authorization_runs_off_event_loop(
    tmp_path, monkeypatch, action, args
):
    import threading

    agent, _, _ = _agent(tmp_path)
    loop_thread = threading.get_ident()
    worker_threads = []

    def deny_path(path, *, operation):
        worker_threads.append(threading.get_ident())
        return ScopeDecision(False, None, None, "blocked outside scope")

    def deny_cwd(cwd):
        worker_threads.append(threading.get_ident())
        return ScopeDecision(False, None, None, "blocked outside scope")

    if action == "WRITE_FILE":
        monkeypatch.setattr(agent._execution_scope, "authorize_path", deny_path)
    else:
        monkeypatch.setattr(agent._execution_scope, "authorize_cwd", deny_cwd)

    with pytest.raises(PermissionError, match="outside scope"):
        await step_executor.execute_step(agent, AgentStep(action=action, args=args))

    assert worker_threads and worker_threads[0] != loop_thread


def test_terminal_helper_requires_explicit_cwd():
    from inference.executors.terminal_executor import run_terminal

    with pytest.raises(TypeError, match="cwd"):
        run_terminal("echo blocked")
