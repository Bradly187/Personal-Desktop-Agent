"""Trust-sensitive terminal containment policy (harness hardening R5/R7.6)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.command_executor import Command, CommandExecutor
from core.execution_policy import ExecutionScope
from inference.dev_agent import DevAgent
import inference.sandbox as sb
from inference.step_executor import _taint_terminal_work
from inference.tester import Tester
from monitoring.metrics import get_metrics


@pytest.mark.parametrize(
    "source,expected",
    [
        ("voice", sb.WorkTrust.TRUSTED_MANUAL),
        ("chat", sb.WorkTrust.TRUSTED_MANUAL),
        ("manual", sb.WorkTrust.TRUSTED_MANUAL),
        ("schedule", sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
        ("event_rule:7", sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
        ("proxy", sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
        (None, sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
        ("", sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
        ("unexpected", sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED),
    ],
)
def test_work_trust_mapping_is_conservative(source, expected):
    assert sb.work_trust_from_source(source) is expected


def test_untrusted_input_monotonically_taints_manual_run():
    assert sb.combine_work_trust(
        sb.WorkTrust.TRUSTED_MANUAL,
        sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED,
    ) is sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    agent = SimpleNamespace(_active_work_trust=sb.WorkTrust.TRUSTED_MANUAL)
    _taint_terminal_work(agent)
    assert agent._active_work_trust is sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED


@pytest.mark.parametrize("tool", ["bwrap", "firejail"])
def test_native_jail_tools_report_namespace_level(monkeypatch, tool):
    monkeypatch.setattr(sb, "_POSIX", True)
    monkeypatch.setattr(sb, "sandbox_tool", lambda: tool)
    report = sb.detect_containment("pytest -q", "/project")
    assert report == sb.ContainmentReport(
        sb.ContainmentLevel.NAMESPACE_JAIL, tool, None
    )


@pytest.mark.parametrize(
    "level,manual_allowed,autonomous_allowed",
    [
        (sb.ContainmentLevel.NATIVE_UNCONFINED, True, False),
        (sb.ContainmentLevel.CWD_AND_RESOURCE_LIMITS, True, False),
        (sb.ContainmentLevel.NAMESPACE_JAIL, True, True),
    ],
)
def test_trust_by_containment_admission_matrix(
    monkeypatch, level, manual_allowed, autonomous_allowed
):
    monkeypatch.setattr(sb.ContainmentPolicy, "_publish_metrics", lambda *a: None)
    report = sb.ContainmentReport(level, "test", "reason")
    for trust, allowed in (
        (sb.WorkTrust.TRUSTED_MANUAL, manual_allowed),
        (sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED, autonomous_allowed),
    ):
        policy = sb.ContainmentPolicy()
        if allowed:
            policy.require(report, trust)
        else:
            with pytest.raises(sb.ContainmentDeniedError):
                policy.require(report, trust)


def test_quiet_notice_occurs_once_per_degraded_state_transition(monkeypatch):
    monkeypatch.setattr(sb.ContainmentPolicy, "_publish_metrics", lambda *a: None)
    policy = sb.ContainmentPolicy()
    notices: list[tuple[str, str]] = []
    policy.set_notice_sink(lambda title, body: notices.append((title, body)))
    native = sb.ContainmentReport(
        sb.ContainmentLevel.NATIVE_UNCONFINED,
        "windows_native",
        "WSL unavailable",
    )
    jailed = sb.ContainmentReport(
        sb.ContainmentLevel.NAMESPACE_JAIL, "wsl+bwrap", None
    )

    policy.require(native, sb.WorkTrust.TRUSTED_MANUAL)
    policy.require(native, sb.WorkTrust.TRUSTED_MANUAL)
    assert len(notices) == 1
    assert "manual" in notices[0][1].lower()

    policy.require(jailed, sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED)
    policy.require(native, sb.WorkTrust.TRUSTED_MANUAL)
    assert len(notices) == 2


def test_denial_notice_is_quiet_content_and_deduplicated(monkeypatch):
    monkeypatch.setattr(sb.ContainmentPolicy, "_publish_metrics", lambda *a: None)
    policy = sb.ContainmentPolicy()
    notices: list[tuple[str, str]] = []
    policy.set_notice_sink(lambda title, body: notices.append((title, body)))
    report = sb.ContainmentReport(
        sb.ContainmentLevel.CWD_AND_RESOURCE_LIMITS,
        "posix+rlimits",
        "no namespace tool",
    )

    for _ in range(2):
        with pytest.raises(sb.ContainmentDeniedError):
            policy.require(report, sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED)
    assert notices == [
        (
            "Terminal isolation reduced",
            "Approved manual terminal work may continue with the current fallback; "
            "automated terminal work is blocked until namespace isolation is available.",
        )
    ]


def test_cached_report_is_visible_in_metrics_and_runtime_status():
    policy = sb.ContainmentPolicy()
    report = sb.ContainmentReport(
        sb.ContainmentLevel.CWD_AND_RESOURCE_LIMITS,
        "posix+rlimits",
        "no namespace tool",
    )
    policy.observe(report, sb.WorkTrust.TRUSTED_MANUAL)
    snapshot = get_metrics().get_snapshot()
    assert snapshot["gauges"]["containment_level"] == 1.0
    assert snapshot["component_status"]["containment"] == report.to_dict()

    router = MagicMock()
    router.get_status.return_value = {}
    agent = DevAgent(router=router, containment_policy=policy)
    assert agent.get_status()["containment"] == report.to_dict()


@pytest.mark.asyncio
async def test_dev_agent_handle_maps_source_into_plan_trust():
    router = MagicMock()
    agent = DevAgent(router=router)
    agent._classifier.classify = MagicMock(return_value="plan")
    agent.plan_and_run = AsyncMock(return_value=MagicMock())

    await agent.handle("do work", source="chat")
    assert agent.plan_and_run.await_args.kwargs["work_trust"] is sb.WorkTrust.TRUSTED_MANUAL

    await agent.handle("do work")
    assert agent.plan_and_run.await_args.kwargs["work_trust"] is sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED


def test_direct_command_executor_unknown_source_denies_before_workload(
    tmp_path, monkeypatch
):
    policy = sb.ContainmentPolicy()
    scope = ExecutionScope(tmp_path, [tmp_path])
    executor = CommandExecutor(scope, containment_policy=policy)
    report = sb.ContainmentReport(
        sb.ContainmentLevel.NATIVE_UNCONFINED,
        "windows_native",
        "no namespace jail",
    )
    monkeypatch.setattr(
        sb,
        "_select_containment",
        lambda command, project_dir: sb._ContainmentRoute(
            report, "native_fallback", project_dir
        ),
    )
    launched = False

    def fail_if_launched(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("workload launched")

    monkeypatch.setattr(sb, "run_capped", fail_if_launched)
    cmd = Command(
        text="echo hi",
        action="RUN_TERMINAL",
        source="proxy",
        params={"command": "echo hi", "cwd": str(tmp_path)},
    )
    result = executor._dispatch("RUN_TERMINAL", cmd)
    assert result["status"] == "error"
    assert "requires namespace_jail" in result["error"]
    assert result["sandboxed"] is False
    assert result["containment"] == report.to_dict()
    assert launched is False


def test_direct_command_executor_manual_source_keeps_fallback(tmp_path, monkeypatch):
    policy = sb.ContainmentPolicy()
    scope = ExecutionScope(tmp_path, [tmp_path])
    executor = CommandExecutor(scope, containment_policy=policy)
    report = sb.ContainmentReport(
        sb.ContainmentLevel.NATIVE_UNCONFINED,
        "windows_native",
        "no namespace jail",
    )
    monkeypatch.setattr(
        sb,
        "_select_containment",
        lambda command, project_dir: sb._ContainmentRoute(
            report, "native_fallback", project_dir
        ),
    )
    monkeypatch.setattr(
        sb,
        "run_capped",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "ok\n", ""
        ),
    )
    cmd = Command(
        text="echo hi",
        action="RUN_TERMINAL",
        source="voice",
        params={"command": "echo hi", "cwd": str(tmp_path)},
    )
    result = executor._dispatch("RUN_TERMINAL", cmd)
    assert result["stdout"] == "ok"
    assert result["containment"] == report.to_dict()


def test_autonomous_tester_requires_namespace_jail(tmp_path, monkeypatch):
    policy = sb.ContainmentPolicy()
    tester = Tester(
        MagicMock(),
        execution_scope=ExecutionScope(tmp_path, [tmp_path]),
        containment_policy=policy,
    )
    report = sb.ContainmentReport(
        sb.ContainmentLevel.NATIVE_UNCONFINED,
        "windows_native",
        "no namespace jail",
    )
    monkeypatch.setattr(
        sb,
        "_select_containment",
        lambda command, project_dir: sb._ContainmentRoute(
            report, "native_fallback", project_dir
        ),
    )
    monkeypatch.setattr(
        sb, "run_capped", lambda *a, **k: pytest.fail("workload launched")
    )
    test_file = tmp_path / "test_generated.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    with pytest.raises(sb.ContainmentDeniedError):
        tester._run_pytest(str(test_file), str(tmp_path))


def test_proxy_receiver_report_updates_sender_status_cache(monkeypatch):
    monkeypatch.setattr(sb.ContainmentPolicy, "_publish_metrics", lambda *a: None)
    from core.action_lifecycle import ActionOutcome, ActionStatus

    policy = sb.ContainmentPolicy()
    executor = CommandExecutor(containment_policy=policy)
    report = sb.ContainmentReport(
        sb.ContainmentLevel.NAMESPACE_JAIL, "wsl+bwrap", None
    )
    cmd = Command(text="pytest", action="RUN_TERMINAL", source="voice")
    outcome = ActionOutcome(
        ActionStatus.COMPLETED,
        "RUN_TERMINAL",
        "trace",
        None,
        None,
        "action",
        result={"containment": report.to_dict()},
    )

    executor._observe_proxy_containment(cmd, outcome)
    assert policy.cached_report() == report


def test_same_containment_state_notice_covers_both_trust_outcomes(monkeypatch):
    monkeypatch.setattr(sb.ContainmentPolicy, "_publish_metrics", lambda *a: None)
    policy = sb.ContainmentPolicy()
    notices: list[tuple[str, str]] = []
    policy.set_notice_sink(lambda title, body: notices.append((title, body)))
    report = sb.ContainmentReport(
        sb.ContainmentLevel.NATIVE_UNCONFINED,
        "windows_native",
        "WSL unavailable",
    )

    policy.require(report, sb.WorkTrust.TRUSTED_MANUAL)
    with pytest.raises(sb.ContainmentDeniedError):
        policy.require(report, sb.WorkTrust.AUTONOMOUS_OR_UNTRUSTED)
    assert len(notices) == 1
    assert "manual" in notices[0][1].lower()
    assert "automated" in notices[0][1].lower()


def test_tester_command_can_route_through_windows_wsl_jail(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_POSIX", False)
    monkeypatch.setattr(
        sb,
        "_wsl_routing_config",
        lambda: {"enabled": True, "distro": "Ubuntu"},
    )
    monkeypatch.setattr(sb, "wsl_available", lambda distro="": True)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")

    monkeypatch.setattr(sb, "run_capped", fake_run)
    # A real directory on whatever drive the host has (CI runners have no E:).
    project = tmp_path / "project"
    project.mkdir()
    tester = Tester(
        MagicMock(),
        execution_scope=ExecutionScope(project, [project]),
    )
    test_path = tmp_path / "test_gen_x.py"
    test_path.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    result = tester._run_pytest(str(test_path), str(project))
    assert result.returncode == 0
    assert result.containment.level is sb.ContainmentLevel.NAMESPACE_JAIL
    command = captured["argv"][-1]
    assert command.startswith('"$HOME/.venv-wsl/bin/python" -c ')
    assert ".exe" not in command.lower()
    assert captured["env"]["PDA_TEST_SOURCE_B64"]
