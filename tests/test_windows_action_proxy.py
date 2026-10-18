"""Replay-resistant, narrowing-preserving WSL-to-Windows proxy tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import core.command_executor as command_executor
import windows_action_proxy as proxy
from core.command_executor import Command, CommandExecutor
from core.execution_policy import ExecutionScope
from core.proxy_auth import (
    HEADER_INSTANCE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    body_sha256,
    canonical_json_bytes,
    request_signature,
)

_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


async def _client() -> TestClient:
    client = TestClient(TestServer(proxy.build_app(_TOKEN)))
    await client.start_server()
    return client


def _payload(cmd: Command, marker: dict | None = None) -> dict:
    payload = {
        "text": cmd.text,
        "action": cmd.action,
        "source": cmd.source,
        "params": cmd.params,
        "whisper_logprob": cmd.whisper_logprob,
        "gesture_confidence": cmd.gesture_confidence,
        "gaze_coords": list(cmd.gaze_coords) if cmd.gaze_coords else None,
        "session_context": cmd.session_context,
    }
    if marker is not None:
        payload["_execution_scope"] = marker
    return payload


def _signed_body_headers(
    client: TestClient,
    payload: dict,
    *,
    token: str = _TOKEN,
    instance_id: str | None = None,
    timestamp: str | None = None,
    nonce: str = "abcdef0123456789abcdef0123456789",
    path: str = "/execute",
) -> tuple[bytes, dict[str, str]]:
    payload = dict(payload)
    if path == "/execute":
        payload.setdefault("version", 2)
        payload.setdefault(
            "action_id", hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        )
        payload.setdefault("trace_id", "trace-test")
        payload.setdefault("run_id", 7)
        payload.setdefault("step_num", 3)
        payload.setdefault("wait_timeout_s", 30.0)
    body = canonical_json_bytes(payload)
    instance = instance_id or client.server.app[proxy._INSTANCE_KEY]
    request_time = timestamp or str(int(time.time()))
    signature = request_signature(
        token,
        method="POST",
        path=path,
        body_hash=body_sha256(body),
        timestamp=request_time,
        nonce=nonce,
        instance_id=instance,
    )
    return body, {
        "Content-Type": "application/json",
        HEADER_INSTANCE: instance,
        HEADER_TIMESTAMP: request_time,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
    }


async def _signed_post(
    client: TestClient,
    payload: dict,
    **auth,
):
    body, headers = _signed_body_headers(client, payload, **auth)
    return await client.post("/execute", data=body, headers=headers)


def test_proxy_refuses_to_start_without_authentication_token(monkeypatch):
    monkeypatch.delenv("WSL_ACTION_PROXY_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="at least 32"):
        proxy.build_app()


def test_proxy_bootstrap_environment_is_consumed(monkeypatch):
    monkeypatch.setenv("WSL_ACTION_PROXY_TOKEN", _TOKEN)
    monkeypatch.setenv("WSL_ACTION_PROXY_INSTANCE", "stale-instance")
    app = proxy.build_app()

    assert app[proxy._TOKEN_KEY] == _TOKEN
    assert len(app[proxy._INSTANCE_KEY]) == 32
    assert "WSL_ACTION_PROXY_TOKEN" not in os.environ
    assert "WSL_ACTION_PROXY_INSTANCE" not in os.environ


async def test_health_is_open_but_unsigned_execute_never_reaches_executor(
    tmp_path, monkeypatch
):
    client = await _client()
    target = tmp_path / "untrusted.txt"
    payload = _payload(Command(
        text="x", action="WRITE_FILE", source="test",
        params={"path": str(target), "content": "x"},
    ), {
        "version": 1,
        "action": "WRITE_FILE",
        "root": str(tmp_path),
        "canonical_path": str(target),
    })
    get_executor = MagicMock(side_effect=AssertionError("executor reached"))
    monkeypatch.setattr(proxy, "_get_executor", get_executor)
    try:
        health = await client.get("/health")
        assert health.status == 200

        missing = await client.post("/execute", json=payload)
        wrong = await _signed_post(client, payload, token="wrong-token")
        assert missing.status == 401
        assert wrong.status == 401
        assert not target.exists()
        get_executor.assert_not_called()
    finally:
        await client.close()


@pytest.mark.parametrize("failure", ["wrong_signature", "replay", "stale", "instance"])
async def test_request_signature_rejects_wrong_replay_stale_and_instance_mismatch(
    monkeypatch, failure
):
    calls = []

    class Executor:
        async def execute(self, cmd):
            calls.append(cmd.action)
            return {"status": "ok", "action": cmd.action}

    monkeypatch.setattr(proxy, "_executor_for_request", lambda data, cmd: Executor())
    client = await _client()
    payload = {"action": "EXPLAIN", "params": {}}
    try:
        if failure == "wrong_signature":
            response = await _signed_post(client, payload, token="wrong-token")
        elif failure == "stale":
            response = await _signed_post(
                client, payload, timestamp=str(int(time.time()) - 31)
            )
        elif failure == "instance":
            response = await _signed_post(client, payload, instance_id="0" * 32)
        else:
            body, headers = _signed_body_headers(client, payload)
            first = await client.post("/execute", data=body, headers=headers)
            assert first.status == 200
            response = await client.post("/execute", data=body, headers=headers)
            assert calls == ["EXPLAIN"]

        assert response.status == 401
        assert (await response.json())["error"] == "unauthorized"
        if failure != "replay":
            assert calls == []
    finally:
        await client.close()


async def test_valid_request_signature_executes_once(monkeypatch):
    class Executor:
        async def execute(self, cmd):
            return {"status": "ok", "action": cmd.action}

    called = MagicMock(side_effect=lambda data, cmd: Executor())
    monkeypatch.setattr(proxy, "_executor_for_request", called)
    client = await _client()
    try:
        response = await _signed_post(
            client, {"action": "EXPLAIN", "params": {}}, nonce="1" * 32
        )
        assert response.status == 200
        assert (await response.json())["status"] == "ok"
        called.assert_called_once()
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows receiver path semantics")
async def test_sender_confirmed_dynamic_root_executes_file_and_terminal_through_receiver(
    tmp_path, monkeypatch
):
    from inference import sandbox

    repository = tmp_path / "repository"
    dynamic = tmp_path / "confirmed-project"
    repository.mkdir()
    dynamic.mkdir()
    scope = ExecutionScope(repository, [repository])
    assert scope.set_active_root(str(dynamic), human_confirmed=True).allowed
    sender = CommandExecutor(scope)
    write = Command(
        text="approved", action="WRITE_FILE", source="test",
        params={"path": "proxied.txt", "content": "approved"},
    )
    forwarded_write, write_marker, denial = sender._proxy_scope_preflight(write)
    assert denial is None and forwarded_write is not None and write_marker is not None
    assert write_marker["root"] == str(dynamic)

    sandbox_calls = []

    class SandboxResult:
        stdout = "terminal ok"
        stderr = ""
        returncode = 0
        sandboxed = True

        containment = None

    def run_sandboxed(
        command, *, project_dir, timeout, allow_network, **kwargs
    ):
        sandbox_calls.append((command, project_dir, timeout, allow_network))
        return SandboxResult()

    monkeypatch.setattr(sandbox, "run_sandboxed", run_sandboxed)
    monkeypatch.setattr("core.command_executor._get_action_verifier", lambda: None)
    client = await _client()
    try:
        write_response = await _signed_post(
            client, _payload(forwarded_write, write_marker), nonce="2" * 32
        )
        write_body = await write_response.json()
        assert write_response.status == 200
        assert write_body["status"] == "ok"
        assert (dynamic / "proxied.txt").read_text(encoding="utf-8") == "approved"

        terminal = Command(
            text="echo ok", action="RUN_TERMINAL", source="voice",
            params={"command": "echo ok"},
        )
        forwarded_terminal, terminal_marker, denial = sender._proxy_scope_preflight(terminal)
        assert denial is None and forwarded_terminal is not None and terminal_marker is not None
        terminal_response = await _signed_post(
            client, _payload(forwarded_terminal, terminal_marker), nonce="3" * 32
        )
        terminal_body = await terminal_response.json()
        assert terminal_response.status == 200
        assert terminal_body["result"]["stdout"] == "terminal ok"
        assert sandbox_calls == [("echo ok", str(dynamic), 22.5, False)]
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows receiver path semantics")
@pytest.mark.parametrize("tamper", ["missing", "action", "escape"])
async def test_signed_proxy_rejects_invalid_or_widened_preauthorization(
    tmp_path, tamper
):
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    target = root / "target.txt"
    marker = {
        "version": 1,
        "action": "WRITE_FILE",
        "root": str(root),
        "canonical_path": str(target),
    }
    params = {"path": str(target), "content": "blocked"}
    request_marker = marker
    if tamper == "missing":
        request_marker = None
    elif tamper == "action":
        request_marker = {**marker, "action": "RUN_TERMINAL"}
    else:
        escaped = sibling / "escaped.txt"
        params["path"] = str(escaped)
        request_marker = {**marker, "canonical_path": str(escaped)}

    client = await _client()
    try:
        response = await _signed_post(
            client,
            _payload(Command(
                text="blocked", action="WRITE_FILE", source="test", params=params,
            ), request_marker),
        )
        assert response.status == 403
        assert not target.exists()
        assert not (sibling / "escaped.txt").exists()
    finally:
        await client.close()


async def test_readiness_proof_includes_random_server_instance(monkeypatch):
    probe = MagicMock(return_value=object())
    monkeypatch.setattr(proxy, "_probe_executor_ready", probe)
    first = await _client()
    second = await _client()
    try:
        ready = await first.get("/ready?challenge=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        body = await ready.json()
        second_instance = second.server.app[proxy._INSTANCE_KEY]

        assert ready.status == 200
        assert body["instance_id"] == first.server.app[proxy._INSTANCE_KEY]
        assert body["instance_id"] != second_instance
        assert body["proof"] == proxy._readiness_proof(
            _TOKEN, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", body["instance_id"]
        )
        assert body["proof"] != proxy._readiness_proof(
            _TOKEN, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", second_instance
        )
        probe.assert_called_once_with()
    finally:
        await first.close()
        await second.close()


async def test_readiness_reports_executor_probe_failure(monkeypatch):
    monkeypatch.setattr(
        proxy, "_probe_executor_ready",
        MagicMock(side_effect=RuntimeError("desktop tools unavailable")),
    )
    client = await _client()
    try:
        response = await client.get("/ready?challenge=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        body = await response.json()
        assert response.status == 503
        assert body["ready"] is False
        assert "desktop tools unavailable" in body["error"]
    finally:
        await client.close()


async def test_proxy_rejects_signed_non_object_params_as_bad_request():
    client = await _client()
    try:
        response = await _signed_post(
            client, {"action": "WRITE_FILE", "params": ["not", "an", "object"]}
        )
        assert response.status == 400
        assert "params must be an object" in (await response.json())["error"]
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows receiver path semantics")
@pytest.mark.parametrize("noncanonical_field", ["root", "canonical_path"])
async def test_proxy_rejects_strings_changed_by_canonical_resolution_before_executor(
    tmp_path, monkeypatch, noncanonical_field
):
    root = tmp_path / "root"
    nested = root / "nested"
    root.mkdir()
    nested.mkdir()
    target = root / "target.txt"
    marker = {
        "version": 1,
        "action": "WRITE_FILE",
        "root": str(root),
        "canonical_path": str(target),
    }
    params = {"path": str(target), "content": "blocked"}
    if noncanonical_field == "root":
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        marker["root"] = str(root / ".." / "sibling")
        marker["canonical_path"] = str(sibling / "target.txt")
        params["path"] = marker["canonical_path"]
    else:
        marker["canonical_path"] = str(nested / ".." / "target.txt")
        params["path"] = marker["canonical_path"]

    construct = MagicMock(side_effect=AssertionError("request executor constructed"))
    monkeypatch.setattr("core.command_executor.CommandExecutor", construct)
    client = await _client()
    try:
        response = await _signed_post(
            client,
            _payload(Command(
                text="blocked", action="WRITE_FILE", source="test", params=params,
            ), marker),
        )
        assert response.status == 403
        assert "canonical" in (await response.json())["error"]
        construct.assert_not_called()
        assert not target.exists()
    finally:
        await client.close()


async def test_proxy_scope_validation_runs_off_event_loop(monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    worker_threads = []

    class Executor:
        async def execute(self, cmd):
            return {"status": "ok", "action": cmd.action}

    def executor_for_request(data, cmd):
        worker_threads.append(threading.get_ident())
        return Executor()

    monkeypatch.setattr(proxy, "_executor_for_request", executor_for_request)
    client = await _client()
    try:
        response = await _signed_post(
            client, {"action": "EXPLAIN", "params": {}}, nonce="4" * 32
        )
        assert response.status == 200
        assert worker_threads and worker_threads[0] != loop_thread
    finally:
        await client.close()


async def test_replacement_listener_captures_no_bearer_or_reusable_key(monkeypatch):
    captured = {}

    async def capture(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.read()
        payload = json.loads(captured["body"])
        return web.json_response({
            "version": 2,
            "status": "ok",
            "lifecycle_status": "completed",
            "action": payload["action"],
            "action_id": payload["action_id"],
            "trace_id": payload["trace_id"],
            "run_id": payload["run_id"],
            "step_num": payload["step_num"],
            "result": {"captured": True},
        })

    app = web.Application()
    app.router.add_post("/execute", capture)
    listener = TestServer(app)
    await listener.start_server()
    instance_id = "5" * 32
    monkeypatch.setattr(command_executor, "_PROXY_URL", str(listener.make_url("" )).rstrip("/"))
    monkeypatch.setattr(command_executor, "_PROXY_TOKEN", _TOKEN)
    monkeypatch.setattr(command_executor, "_PROXY_INSTANCE_ID", instance_id)
    try:
        result = await CommandExecutor()._proxy_execute(
            Command(
                text="explain", action="EXPLAIN", source="test", params={},
                action_id="capture-" + "5" * 40,
            ),
            None,
        )
        assert result["status"] == "ok"
        assert result["result"] == {"captured": True}
        assert "X-Agent-Token" not in captured["headers"]
        assert _TOKEN.encode() not in captured["body"]
        assert _TOKEN not in json.dumps(captured["headers"])
        assert captured["headers"][HEADER_INSTANCE] == instance_id
        assert captured["headers"][HEADER_SIGNATURE]

        replacement = await _client()
        try:
            replay = await replacement.post(
                "/execute", data=captured["body"], headers=captured["headers"]
            )
            assert replay.status == 401
        finally:
            await replacement.close()
    finally:
        await listener.close()


def test_command_executor_module_capture_removes_child_environment_credentials():
    env = os.environ.copy()
    env.update({
        "WSL_ACTION_PROXY": "http://127.0.0.1:8768",
        "WSL_ACTION_PROXY_TOKEN": _TOKEN,
        "WSL_ACTION_PROXY_INSTANCE": "6" * 32,
    })
    code = (
        "import json,os; import core.command_executor as c; "
        "print(json.dumps({"
        "'token': c._PROXY_TOKEN, 'instance': c._PROXY_INSTANCE_ID, "
        "'env_token': os.environ.get('WSL_ACTION_PROXY_TOKEN'), "
        "'env_instance': os.environ.get('WSL_ACTION_PROXY_INSTANCE')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    captured = json.loads(result.stdout.strip().splitlines()[-1])
    assert captured == {
        "token": _TOKEN,
        "instance": "6" * 32,
        "env_token": None,
        "env_instance": None,
    }


def test_batch_uses_external_acl_secret_and_parses_instance_bound_proof():
    script = (Path(__file__).parents[1] / "start_windows_proxy.bat").read_text(
        encoding="utf-8"
    )

    assert "..\\Personal_Desktop_Agent-secrets" in script
    assert "logs\\windows_proxy.token" not in script
    assert script.count("scripts\\Set-ProxySecretAcl.ps1") == 2
    assert "icacls" not in script
    acl_script = (Path(__file__).parents[1] / "scripts" / "Set-ProxySecretAcl.ps1").read_text(
        encoding="utf-8"
    )
    assert "$acl.SetAccessRuleProtection($true, $false)" in acl_script
    assert "unexpected principal" in acl_script
    assert "scripts\\verify_proxy_ready.py" in script
    assert "instance_id plus HMAC(token, challenge + instance_id)" in script
    assert "X-Agent-Token" not in script
    assert "setlocal" in script


def test_wsl_launcher_verifies_proof_and_scopes_bootstrap_env_to_python():
    script = (Path(__file__).parents[1] / "start_agent_wsl.sh").read_text(
        encoding="utf-8"
    )

    assert '${MOUNT_POINT}/Personal_Desktop_Agent-secrets/windows_proxy.token' in script
    assert '${PROJ_ROOT}/logs/windows_proxy.token' not in script
    assert "scripts/verify_proxy_ready.py" in script
    assert "/ready?challenge=${WSL_PROXY_CHALLENGE}" in script
    assert 'unset WSL_ACTION_PROXY_TOKEN WSL_ACTION_PROXY_INSTANCE' in script
    assert 'WSL_ACTION_PROXY_TOKEN="$PROXY_BOOTSTRAP_TOKEN" \\\nWSL_ACTION_PROXY_INSTANCE="$PROXY_BOOTSTRAP_INSTANCE" \\\npython main.py' in script
    assert 'if [[ "$PROXY_READY" -eq 1 ]]; then\n    export WSL_ACTION_PROXY=' in script


def test_readiness_verifier_accepts_only_instance_bound_proof(tmp_path):
    challenge = "c" * 32
    instance_id = "7" * 32
    response = tmp_path / "ready.json"
    response.write_text(json.dumps({
        "ready": True,
        "instance_id": instance_id,
        "proof": proxy._readiness_proof(_TOKEN, challenge, instance_id),
    }), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "WSL_ACTION_PROXY_TOKEN": _TOKEN,
        "WSL_PROXY_CHALLENGE": challenge,
    })
    verifier = Path(__file__).parents[1] / "scripts" / "verify_proxy_ready.py"

    valid = subprocess.run(
        [sys.executable, str(verifier), str(response)], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert valid.returncode == 0
    assert valid.stdout.strip() == instance_id

    response.write_text(json.dumps({
        "ready": True,
        "instance_id": "8" * 32,
        "proof": proxy._readiness_proof(_TOKEN, challenge, instance_id),
    }), encoding="utf-8")
    invalid = subprocess.run(
        [sys.executable, str(verifier), str(response)], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert invalid.returncode != 0
    assert invalid.stdout == ""


def test_authenticated_nonce_cache_is_bounded():
    app = proxy.build_app(_TOKEN)
    for index in range(proxy.NONCE_CACHE_LIMIT + 5):
        proxy._remember_nonce(app, f"{index:032x}", index)

    cache = app[proxy._NONCE_CACHE_KEY]
    assert len(cache) == proxy.NONCE_CACHE_LIMIT
    assert f"{0:032x}" not in cache
    assert f"{proxy.NONCE_CACHE_LIMIT + 4:032x}" in cache


async def test_readiness_rejects_execute_signature_prefix_oracle(monkeypatch):
    probe = MagicMock(side_effect=AssertionError("invalid challenge reached probe"))
    monkeypatch.setattr(proxy, "_probe_executor_ready", probe)
    client = await _client()
    body = canonical_json_bytes({"action": "EXPLAIN", "params": {}})
    forged_prefix = "\n".join((
        "POST",
        "/execute",
        body_sha256(body),
        str(int(time.time())),
        "9" * 32,
        "",
    ))
    assert len(forged_prefix) > 32
    try:
        response = await client.get("/ready", params={"challenge": forged_prefix})
        assert response.status == 400
        probe.assert_not_called()
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_secret_acl_helper_replaces_preexisting_explicit_grants(tmp_path):
    secret_dir = tmp_path / "proxy-secrets"
    token_file = secret_dir / "windows_proxy.token"
    secret_dir.mkdir()
    token_file.write_text("not-a-real-token", encoding="utf-8")

    for path, grant in (
        (secret_dir, "*S-1-1-0:(OI)(CI)(R)"),
        (token_file, "*S-1-1-0:(R)"),
    ):
        subprocess.run(
            ["icacls", str(path), "/grant", grant],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

    acl_script = Path(__file__).parents[1] / "scripts" / "Set-ProxySecretAcl.ps1"
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(acl_script),
            "-Path", str(secret_dir), "-Directory",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(acl_script),
            "-Path", str(token_file),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    query = (
        "$acl=Get-Acl -LiteralPath $env:ACL_TEST_PATH;"
        "$sids=@($acl.Access | ForEach-Object {"
        "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value});"
        "[pscustomobject]@{Protected=$acl.AreAccessRulesProtected;Sids=$sids} | "
        "ConvertTo-Json -Compress"
    )
    current_sid = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout.strip()
    for path in (secret_dir, token_file):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            env={**os.environ, "ACL_TEST_PATH": str(path)},
            capture_output=True, text=True, timeout=15, check=True,
        )
        acl = json.loads(result.stdout)
        assert acl["Protected"] is True
        sids = acl["Sids"] if isinstance(acl["Sids"], list) else [acl["Sids"]]
        assert set(sids) == {current_sid}


async def test_simultaneous_signed_replay_executes_exactly_once():
    app = proxy.build_app(_TOKEN)
    instance_id = app[proxy._INSTANCE_KEY]
    payload = {"action": "EXPLAIN", "params": {}}
    body = canonical_json_bytes(payload)
    timestamp = str(int(time.time()))
    nonce = "d" * 32
    headers = {
        HEADER_INSTANCE: instance_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: request_signature(
            _TOKEN,
            method="POST",
            path="/execute",
            body_hash=body_sha256(body),
            timestamp=timestamp,
            nonce=nonce,
            instance_id=instance_id,
        ),
    }
    both_reads_started = asyncio.Event()
    read_count = 0
    executed = 0

    class ConcurrentRequest:
        path = "/execute"
        method = "POST"
        remote = "127.0.0.1"

        def __init__(self):
            self.app = app
            self.headers = headers

        async def read(self):
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                both_reads_started.set()
            await both_reads_started.wait()
            return body

    async def handler(request):
        nonlocal executed
        executed += 1
        await asyncio.sleep(0)
        return web.json_response({"status": "ok"})

    first, second = await asyncio.gather(
        proxy.authenticate(ConcurrentRequest(), handler),
        proxy.authenticate(ConcurrentRequest(), handler),
    )

    assert sorted((first.status, second.status)) == [200, 401]
    assert executed == 1


async def _signed_result_post(
    client: TestClient, identity: dict, *, nonce: str, instance_id: str | None = None
):
    body, headers = _signed_body_headers(
        client,
        identity,
        path="/result",
        nonce=nonce,
        instance_id=instance_id,
    )
    return await client.post("/result", data=body, headers=headers)


async def test_execute_timeout_late_result_query_and_duplicate_idempotency(monkeypatch):
    release = asyncio.Event()
    calls = 0

    class Executor:
        async def execute(self, cmd):
            nonlocal calls
            calls += 1
            await release.wait()
            return {
                "status": "ok",
                "lifecycle_status": "completed",
                "action": cmd.action,
                "action_id": cmd.action_id,
                "trace_id": cmd.trace_id,
                "run_id": cmd.run_id,
                "step_num": cmd.step_num,
                "result": {"late": True},
            }

    monkeypatch.setattr(proxy, "_executor_for_request", lambda data, cmd: Executor())
    client = await _client()
    identity = {
        "version": 2,
        "action_id": "late-" + "1" * 40,
        "trace_id": "trace-original",
        "run_id": 22,
        "step_num": 5,
        "action": "WRITE_FILE",
    }
    payload = {
        **identity,
        "source": "test",
        "params": {},
        "wait_timeout_s": 0.01,
    }
    try:
        first = await _signed_post(client, payload, nonce="6" * 32)
        duplicate = await _signed_post(client, payload, nonce="7" * 32)
        assert (await first.json())["lifecycle_status"] == "outcome_unknown"
        assert (await duplicate.json())["lifecycle_status"] == "outcome_unknown"
        assert calls == 1

        pending = await _signed_result_post(client, identity, nonce="8" * 32)
        assert (await pending.json())["lifecycle_status"] == "outcome_unknown"

        release.set()
        await asyncio.sleep(0.02)
        terminal = await _signed_result_post(client, identity, nonce="9" * 32)
        body = await terminal.json()
        assert terminal.status == 200
        assert body["lifecycle_status"] == "completed"
        assert body["trace_id"] == "trace-original"
        assert body["run_id"] == 22 and body["step_num"] == 5
        assert body["result"] == {"late": True}
    finally:
        release.set()
        await client.close()


async def test_result_query_requires_matching_signed_identity_and_instance(monkeypatch):
    class Executor:
        async def execute(self, cmd):
            return {"status": "ok", "action": cmd.action, "result": "done"}

    monkeypatch.setattr(proxy, "_executor_for_request", lambda data, cmd: Executor())
    client = await _client()
    identity = {
        "version": 2,
        "action_id": "auth-" + "2" * 40,
        "trace_id": "trace-authorized",
        "run_id": 3,
        "step_num": 1,
        "action": "EXPLAIN",
    }
    try:
        response = await _signed_post(
            client, {**identity, "params": {}, "wait_timeout_s": 1}, nonce="a" * 32
        )
        assert response.status == 200

        mismatch = await _signed_result_post(
            client, {**identity, "trace_id": "other"}, nonce="b" * 32
        )
        assert mismatch.status == 403

        wrong_instance = await _signed_result_post(
            client, identity, nonce="c" * 32, instance_id="0" * 32
        )
        assert wrong_instance.status == 401
    finally:
        await client.close()


async def test_proxy_registry_bound_denies_second_action_before_execution(monkeypatch):
    release = asyncio.Event()
    calls = 0

    class Executor:
        async def execute(self, cmd):
            nonlocal calls
            calls += 1
            await release.wait()
            return {"status": "ok", "action": cmd.action}

    monkeypatch.setattr(proxy, "_executor_for_request", lambda data, cmd: Executor())
    app = proxy.build_app(_TOKEN)
    app[proxy._ACTION_REGISTRY_KEY] = __import__(
        "core.action_lifecycle", fromlist=["TrackedWorkRegistry"]
    ).TrackedWorkRegistry(limit=1, ttl_s=30)
    client = TestClient(TestServer(app))
    await client.start_server()
    base = {
        "version": 2,
        "trace_id": "t",
        "run_id": 1,
        "step_num": 1,
        "action": "WRITE_FILE",
        "params": {},
        "wait_timeout_s": 0.01,
    }
    try:
        first = await _signed_post(
            client, {**base, "action_id": "bound-" + "3" * 40}, nonce="d" * 32
        )
        second = await _signed_post(
            client, {**base, "action_id": "bound-" + "4" * 40}, nonce="e" * 32
        )
        assert first.status == 200
        assert second.status == 503
        assert calls == 1
    finally:
        release.set()
        await client.close()


async def test_result_query_sees_pending_before_executor_preparation_finishes(monkeypatch):
    import threading

    preparation_started = threading.Event()
    release_preparation = threading.Event()
    calls = 0

    class Executor:
        async def execute(self, cmd):
            nonlocal calls
            calls += 1
            return {"status": "ok", "action": cmd.action, "result": "done"}

    def prepare(data, cmd):
        preparation_started.set()
        release_preparation.wait(2)
        return Executor()

    monkeypatch.setattr(proxy, "_executor_for_request", prepare)
    client = await _client()
    identity = {
        "version": 2,
        "action_id": "prepare-" + "b" * 40,
        "trace_id": "prep-trace",
        "run_id": 31,
        "step_num": 2,
        "action": "EXPLAIN",
    }
    execute_task = asyncio.create_task(_signed_post(
        client,
        {**identity, "params": {}, "wait_timeout_s": 1},
        nonce="1a" * 16,
    ))
    try:
        await asyncio.wait_for(asyncio.to_thread(preparation_started.wait), timeout=1)
        pending = await _signed_result_post(client, identity, nonce="2b" * 16)
        body = await pending.json()
        assert pending.status == 200
        assert body["lifecycle_status"] == "outcome_unknown"

        release_preparation.set()
        executed = await execute_task
        assert executed.status == 200
        assert calls == 1
    finally:
        release_preparation.set()
        if not execute_task.done():
            await execute_task
        await client.close()


@pytest.mark.parametrize("http_status", [400, 401, 403, 503])
async def test_sender_classifies_proxy_rejection_as_failed_not_completed(
    monkeypatch, http_status
):
    executor = CommandExecutor()
    monkeypatch.setattr(command_executor, "_PROXY_TOKEN", _TOKEN)
    monkeypatch.setattr(command_executor, "_PROXY_INSTANCE_ID", "c" * 32)

    async def rejected(_session, path, payload, *, timeout_s):
        assert path == "/execute"
        return http_status, {"status": "error", "error": "rejected before execution"}

    monkeypatch.setattr(executor, "_signed_proxy_post", rejected)
    result = await executor._proxy_execute(
        Command(
            text="x", action="EXPLAIN", source="test",
            action_id=f"reject-{http_status}-" + "d" * 32,
        ),
        None,
        timeout_s=0.1,
    )
    assert result["status"] == "error"
    assert result["lifecycle_status"] == "failed"
    assert "rejected" in result["error"]
