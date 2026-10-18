"""Authenticated Windows desktop-action proxy for the WSL agent."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import secrets
import string
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mcp_server"))

from aiohttp import web

from core.action_lifecycle import (
    ActionIdentity,
    ActionIdentityMismatch,
    RegistryFullError,
    TrackedWorkRegistry,
)
from core.proxy_auth import (
    AUTH_WINDOW_SECONDS,
    HEADER_INSTANCE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    INSTANCE_ENV,
    INSTANCE_HEX_LENGTH,
    NONCE_CACHE_LIMIT,
    NONCE_HEX_LENGTH,
    READINESS_CHALLENGE_HEX_LENGTH,
    TOKEN_ENV,
    body_sha256,
    readiness_proof,
    request_signature,
)

log = logging.getLogger("action_proxy")
_TOKEN_KEY = web.AppKey("windows_action_proxy_token", str)
_INSTANCE_KEY = web.AppKey("windows_action_proxy_instance", str)
_NONCE_CACHE_KEY = web.AppKey("windows_action_proxy_nonces", OrderedDict)
_ACTION_REGISTRY_KEY = web.AppKey("windows_action_proxy_actions", TrackedWorkRegistry)
_SCOPE_KEY = "_execution_scope"
_PATH_ACTIONS = frozenset({"WRITE_FILE", "RUN_TERMINAL"})

# Lazy import — CommandExecutor imports pyautogui/win32gui which must succeed
# here (we're on Windows Python).
_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        from core.command_executor import CommandExecutor
        _executor = CommandExecutor()
    return _executor


class ProxyAuthorizationError(ValueError):
    """The authenticated request did not preserve sender scope narrowing."""


def _canonical_string_unchanged(raw: str, canonical: Path) -> bool:
    """Require the sender's absolute string to already be canonical."""
    return os.path.normcase(raw) == os.path.normcase(str(canonical))


def _probe_executor_ready():
    """Initialize the executor and prove required desktop tools imported."""
    executor = _get_executor()
    from core import command_executor
    if any(tool is None for tool in (
        command_executor.keyboard,
        command_executor.mouse,
        command_executor.screen,
        command_executor.windows,
    )):
        raise RuntimeError("Windows desktop action dependencies are unavailable")
    return executor


def _executor_for_request(
    data: dict[str, Any], cmd, action_registry: TrackedWorkRegistry | None = None
):
    """Return an executor whose scope is no wider than the sender's decision."""
    action = cmd.action.upper()
    marker = data.get(_SCOPE_KEY)
    if action not in _PATH_ACTIONS:
        if marker is not None:
            raise ProxyAuthorizationError("scope preauthorization is invalid for this action")
        executor = _get_executor()
        if action_registry is not None and hasattr(executor, "_action_registry"):
            executor._action_registry = action_registry
        return executor
    if not isinstance(marker, dict):
        raise ProxyAuthorizationError("scope preauthorization is required")
    if marker.get("version") != 1 or marker.get("action") != action:
        raise ProxyAuthorizationError("scope preauthorization does not match the action")

    root = marker.get("root")
    canonical = marker.get("canonical_path")
    field = "path" if action == "WRITE_FILE" else "cwd"
    forwarded = cmd.params.get(field)
    if not all(isinstance(value, str) and value for value in (root, canonical, forwarded)):
        raise ProxyAuthorizationError("scope preauthorization is incomplete")
    if forwarded != canonical:
        raise ProxyAuthorizationError("forwarded path does not match sender authorization")

    root_path = Path(root)
    try:
        if not root_path.is_absolute():
            raise ProxyAuthorizationError("authorized root is not an absolute path")
        resolved_root = root_path.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ProxyAuthorizationError("authorized root is not an existing directory")
    except ProxyAuthorizationError:
        raise
    except OSError as exc:
        raise ProxyAuthorizationError("authorized root could not be verified") from exc
    if not _canonical_string_unchanged(root, resolved_root):
        raise ProxyAuthorizationError("authorized root is not canonical")

    from core.command_executor import CommandExecutor
    from core.execution_policy import ExecutionScope

    scope = ExecutionScope(repository_root=resolved_root, writable_roots=[resolved_root])
    decision = (
        scope.authorize_path(forwarded, operation="WRITE_FILE")
        if action == "WRITE_FILE"
        else scope.authorize_cwd(forwarded)
    )
    if not decision.allowed or decision.canonical_path is None:
        raise ProxyAuthorizationError("forwarded path is outside the authorized root")
    if not _canonical_string_unchanged(canonical, decision.canonical_path):
        raise ProxyAuthorizationError("forwarded path is not canonical")
    return CommandExecutor(scope, action_registry=action_registry)


def _readiness_proof(token: str, challenge: str, instance_id: str) -> str:
    return readiness_proof(token, challenge, instance_id)


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in string.hexdigits for char in value)


def _reject_auth(reason: str) -> web.Response:
    log.warning("Rejected proxy request authentication: %s", reason)
    return web.json_response({"status": "error", "error": "unauthorized"}, status=401)


def _nonce_is_replay(app: web.Application, nonce: str, now: int) -> bool:
    cache = app[_NONCE_CACHE_KEY]
    cutoff = now - AUTH_WINDOW_SECONDS
    while cache:
        _, seen_at = next(iter(cache.items()))
        if seen_at >= cutoff:
            break
        cache.popitem(last=False)
    return nonce in cache


def _remember_nonce(app: web.Application, nonce: str, now: int) -> None:
    cache = app[_NONCE_CACHE_KEY]
    cache[nonce] = now
    while len(cache) > NONCE_CACHE_LIMIT:
        cache.popitem(last=False)


@web.middleware
async def authenticate(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if request.remote not in {"127.0.0.1", "::1"}:
        return web.json_response(
            {"status": "error", "error": "loopback requests only"}, status=403
        )
    # Readiness proves possession without disclosing the bootstrap credential.
    if request.path == "/ready":
        return await handler(request)
    if request.path not in {"/execute", "/result"} or request.method != "POST":
        return _reject_auth("unsupported authenticated route")

    instance_id = request.headers.get(HEADER_INSTANCE, "")
    if not hmac.compare_digest(instance_id, request.app[_INSTANCE_KEY]):
        return _reject_auth("instance mismatch")

    timestamp = request.headers.get(HEADER_TIMESTAMP, "")
    try:
        request_time = int(timestamp)
    except ValueError:
        return _reject_auth("invalid timestamp")
    now = int(time.time())
    if abs(now - request_time) > AUTH_WINDOW_SECONDS:
        return _reject_auth("timestamp outside authentication window")

    nonce = request.headers.get(HEADER_NONCE, "")
    if not _is_hex(nonce, NONCE_HEX_LENGTH):
        return _reject_auth("invalid nonce")

    signature = request.headers.get(HEADER_SIGNATURE, "")
    if not _is_hex(signature, 64):
        return _reject_auth("invalid signature")
    body = await request.read()
    expected = request_signature(
        request.app[_TOKEN_KEY],
        method=request.method,
        path=request.path,
        body_hash=body_sha256(body),
        timestamp=timestamp,
        nonce=nonce,
        instance_id=instance_id,
    )
    if not hmac.compare_digest(signature, expected):
        return _reject_auth("signature mismatch")

    # Check and consume only after authentication. There is no await between
    # lookup and insertion, so concurrent identical requests cannot both pass
    # on aiohttp's single event-loop thread, and invalid traffic cannot poison
    # the bounded cache.
    if _nonce_is_replay(request.app, nonce, now):
        return _reject_auth("nonce replay")
    _remember_nonce(request.app, nonce, now)
    return await handler(request)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "authenticated": True, "pid": os.getpid()})


async def handle_ready(request: web.Request) -> web.Response:
    challenge = request.query.get("challenge", "")
    if not _is_hex(challenge, READINESS_CHALLENGE_HEX_LENGTH):
        return web.json_response(
            {"status": "error", "error": "readiness challenge must be 32 hexadecimal characters"},
            status=400,
        )
    try:
        await asyncio.to_thread(_probe_executor_ready)
    except Exception as exc:
        log.error("proxy readiness failed: %s", exc)
        return web.json_response(
            {"status": "error", "ready": False, "error": str(exc)}, status=503
        )
    token = request.app[_TOKEN_KEY]
    instance_id = request.app[_INSTANCE_KEY]
    return web.json_response({
        "ok": True,
        "ready": True,
        "instance_id": instance_id,
        "proof": _readiness_proof(token, challenge, instance_id),
        "pid": os.getpid(),
    })


def _identity_from_data(data: dict[str, Any]) -> ActionIdentity:
    if data.get("version") != 2:
        raise ValueError("unsupported action proxy contract version")
    action_id = data.get("action_id")
    action = data.get("action")
    trace_id = data.get("trace_id", "")
    run_id = data.get("run_id")
    step_num = data.get("step_num")
    if not isinstance(action_id, str) or not (32 <= len(action_id) <= 128):
        raise ValueError("action_id must be an unguessable 32-128 character string")
    if not isinstance(action, str) or not action or action != action.upper():
        raise ValueError("action identity must contain an uppercase action")
    if not isinstance(trace_id, str):
        raise ValueError("trace_id must be a string")
    for name, value in (("run_id", run_id), ("step_num", step_num)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"{name} must be an integer or null")
    return ActionIdentity(action_id, action, trace_id, run_id, step_num)


async def handle_execute(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
    except Exception as exc:
        return web.json_response({"status": "error", "error": f"bad JSON: {exc}"}, status=400)

    try:
        from core.command_executor import Command
        identity = _identity_from_data(data)
        params = data.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        wait_timeout_s = float(data.get("wait_timeout_s", 30.0))
        if not (0.01 <= wait_timeout_s <= 300.0):
            raise ValueError("wait_timeout_s is outside the supported range")
        cmd = Command(
            text=data.get("text", ""),
            action=identity.action,
            source=data.get("source", "proxy"),
            params=params,
            whisper_logprob=float(data.get("whisper_logprob", 0.0)),
            gesture_confidence=float(data.get("gesture_confidence", 1.0)),
            gaze_coords=tuple(data["gaze_coords"]) if data.get("gaze_coords") else None,
            session_context=data.get("session_context") or [],
            trace_id=identity.trace_id,
            action_id=identity.action_id,
            run_id=identity.run_id,
            step_num=identity.step_num,
        )
        registry = request.app[_ACTION_REGISTRY_KEY]
        loop = asyncio.get_running_loop()
        prepared_executor = loop.create_future()

        async def execute_once():
            executor = await prepared_executor
            if hasattr(executor, "execute_registry_managed"):
                return await executor.execute_registry_managed(cmd)
            return await executor.execute(cmd)

        # Reserve action_id before yielding to blocking scope/executor setup, so
        # any concurrent result query sees a pending identity rather than a
        # misleading absence. Duplicate execute requests never prepare or run a
        # second executor.
        entry, started = await registry.start_or_get(
            identity, execute_once, destructive=True
        )
        if started:
            prepare_task = asyncio.create_task(
                asyncio.to_thread(_executor_for_request, data, cmd)
            )

            def publish_preparation(task):
                if prepared_executor.done():
                    return
                try:
                    prepared_executor.set_result(task.result())
                except BaseException as exc:
                    prepared_executor.set_exception(exc)

            prepare_task.add_done_callback(publish_preparation)
            # Preserve established HTTP authorization/validation responses when
            # this connection remains present. Shield keeps preparation and the
            # retained registry task alive if the client disconnects.
            await asyncio.shield(prepare_task)
        result = await registry.await_entry(entry, timeout_s=wait_timeout_s)
        return web.json_response(result.to_dict())
    except ActionIdentityMismatch as exc:
        return web.json_response({"status": "error", "error": str(exc)}, status=403)
    except RegistryFullError as exc:
        return web.json_response({"status": "error", "error": str(exc)}, status=503)
    except ProxyAuthorizationError as exc:
        log.warning("Rejected proxy scope authorization: %s", exc)
        return web.json_response({"status": "error", "error": str(exc)}, status=403)
    except (TypeError, ValueError) as exc:
        return web.json_response({"status": "error", "error": str(exc)}, status=400)
    except Exception as exc:
        log.error("proxy execute error: %s", exc, exc_info=True)
        return web.json_response({"status": "error", "error": str(exc)}, status=500)


async def handle_result(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        identity = _identity_from_data(data)
        outcome = await request.app[_ACTION_REGISTRY_KEY].query(identity)
        if outcome is None:
            return web.json_response(
                {"status": "error", "error": "action_id not found"}, status=404
            )
        return web.json_response(outcome.to_dict())
    except ActionIdentityMismatch as exc:
        return web.json_response({"status": "error", "error": str(exc)}, status=403)
    except (TypeError, ValueError) as exc:
        return web.json_response({"status": "error", "error": str(exc)}, status=400)


def build_app(proxy_token: str | None = None) -> web.Application:
    # The environment is bootstrap transport only. Remove both values before any
    # executor or child process can be initialized from this server process.
    environment_token = os.environ.pop(TOKEN_ENV, "")
    os.environ.pop(INSTANCE_ENV, None)
    token = (proxy_token if proxy_token is not None else environment_token).strip()
    if len(token) < 32:
        raise RuntimeError("WSL_ACTION_PROXY_TOKEN must contain at least 32 characters")
    app = web.Application(middlewares=[authenticate])
    app[_TOKEN_KEY] = token
    app[_INSTANCE_KEY] = secrets.token_hex(INSTANCE_HEX_LENGTH // 2)
    app[_NONCE_CACHE_KEY] = OrderedDict()
    app[_ACTION_REGISTRY_KEY] = TrackedWorkRegistry(limit=32, ttl_s=300.0)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_post("/execute", handle_execute)
    app.router.add_post("/result", handle_result)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Windows desktop action proxy for WSL agent")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    log.info("Windows Action Proxy starting on http://127.0.0.1:%d", args.port)
    web.run_app(build_app(), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
