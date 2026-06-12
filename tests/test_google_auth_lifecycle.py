"""Gmail OAuth lifecycle — graceful expiry, enabled-state overrides, hot-start,
and the voice "connect Google" flow.

Run:
    python -m pytest tests/test_google_auth_lifecycle.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from skills import google_setup as gs
from skills.servers.google_pim_server import _auth_guard, _NotAuthorized, RECONNECT_MSG
from core.email_watcher import EmailWatcher
from core.hybrid_coordinator import HybridCoordinator, _is_system_control_voice

_MANIFEST_DIR = Path(__file__).parent.parent / "skills" / "manifests"


# ---------------------------------------------------------------------------
# Server: every auth failure mode → one actionable message, never a traceback
# ---------------------------------------------------------------------------

def test_auth_guard_not_authorized():
    @_auth_guard
    def f():
        raise _NotAuthorized(RECONNECT_MSG)
    assert f() == RECONNECT_MSG


def test_auth_guard_midcall_invalid_grant_and_401():
    for msg in ("invalid_grant: Token has been revoked", "HTTP 401 Unauthorized"):
        @_auth_guard
        def f(m=msg):
            raise RuntimeError(m)
        assert f() == RECONNECT_MSG


def test_auth_guard_other_errors_propagate():
    @_auth_guard
    def f():
        raise ValueError("quota exceeded")
    with pytest.raises(ValueError):
        f()


# ---------------------------------------------------------------------------
# google_setup: secret resolution, enabled overrides, blockers, auth flow
# ---------------------------------------------------------------------------

def test_client_secret_env_wins(tmp_path, monkeypatch):
    env_secret = tmp_path / "env.json"; env_secret.write_text("{}")
    default = tmp_path / "default.json"; default.write_text("{}")
    monkeypatch.setattr(gs, "DEFAULT_CLIENT_SECRET", default)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS", str(env_secret))
    assert gs.client_secret_path() == env_secret
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRETS")
    assert gs.client_secret_path() == default
    default.unlink()
    assert gs.client_secret_path() is None


def test_enabled_overrides_roundtrip(tmp_path):
    p = tmp_path / "enabled.json"
    assert gs.load_enabled_overrides(p) == {}
    gs.set_skill_enabled("google_pim", True, path=p)
    gs.set_skill_enabled("echo", False, path=p)
    assert gs.load_enabled_overrides(p) == {"google_pim": True, "echo": False}


def test_setup_blocker_paths(monkeypatch):
    monkeypatch.setattr(gs, "google_libs_available", lambda: False)
    assert "pip install" in gs.setup_blocker()
    monkeypatch.setattr(gs, "google_libs_available", lambda: True)
    monkeypatch.setattr(gs, "client_secret_path", lambda: None)
    assert "client" in gs.setup_blocker().lower()
    monkeypatch.setattr(gs, "client_secret_path", lambda: Path("x"))
    assert gs.setup_blocker() is None


async def test_run_auth_flow_success_and_failure(monkeypatch):
    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", None))
        proc.returncode = 0
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(gs, "has_token", lambda: True)
    ok, msg = await gs.run_auth_flow()
    assert ok and "connected" in msg.lower()

    async def _fail_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"boom: consent denied", None))
        proc.returncode = 1
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_exec)
    ok, msg = await gs.run_auth_flow()
    assert not ok and "consent denied" in msg


# ---------------------------------------------------------------------------
# Registry: enabled overrides + hot-start (real echo server over stdio)
# ---------------------------------------------------------------------------

@pytest.fixture
def _override_path(tmp_path, monkeypatch):
    p = tmp_path / "enabled.json"
    monkeypatch.setattr(gs, "ENABLED_OVERRIDES_PATH", p)
    yield p
    from core.domain_classifier import DomainClassifier
    DomainClassifier.register_skill_keywords(set())


def _echo_manifest_dir(tmp_path) -> Path:
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    manifest = json.loads((_MANIFEST_DIR / "echo.json").read_text(encoding="utf-8"))
    manifest["enabled"] = False
    manifest["server"]["command"] = sys.executable
    (mdir / "echo.json").write_text(json.dumps(manifest), encoding="utf-8")
    return mdir


async def test_override_enables_disabled_manifest(tmp_path, _override_path):
    from skills.registry import SkillRegistry
    mdir = _echo_manifest_dir(tmp_path)
    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    try:
        assert not reg.has_skills()              # manifest disabled, no override
    finally:
        await reg.stop()

    gs.set_skill_enabled("echo", True, path=_override_path)
    reg2 = SkillRegistry(manifest_dir=mdir)
    await reg2.start()
    try:
        assert reg2.has_skills()                 # override wins over manifest
    finally:
        await reg2.stop()


async def test_hot_start_skill_after_enable(tmp_path, _override_path):
    from skills.registry import SkillRegistry
    mdir = _echo_manifest_dir(tmp_path)
    reg = SkillRegistry(manifest_dir=mdir)
    await reg.start()
    try:
        assert not reg.has_skills()
        assert await reg.start_skill("echo") is False     # still disabled
        gs.set_skill_enabled("echo", True, path=_override_path)
        assert await reg.start_skill("echo") is True      # hot-started
        res = await reg.call("echo", "echo_text", {"text": "hot"})
        assert res["status"] == "ok" and "hot" in res["text"]
        assert await reg.start_skill("echo") is True      # idempotent
        assert await reg.start_skill("nope") is False     # unknown id
    finally:
        await reg.stop()


# ---------------------------------------------------------------------------
# EmailWatcher: always-start, skill-absent no-op, one-time expiry alert
# ---------------------------------------------------------------------------

def _registry_with_skill(text: str):
    reg = MagicMock()
    reg.has_skills = MagicMock(return_value=True)
    reg._skills = {"google_pim": object()}
    reg.call = AsyncMock(return_value={"status": "ok", "text": text})
    return reg


async def test_watcher_starts_without_skill_and_ticks_noop():
    reg = MagicMock()
    reg.has_skills = MagicMock(return_value=False)
    reg._skills = {}
    reg.call = AsyncMock()
    w = EmailWatcher(reg, MagicMock(), notifier=MagicMock())
    await w.start()
    try:
        assert w.is_healthy()                 # loop runs, waiting for the skill
        assert await w._tick() == 0
        reg.call.assert_not_awaited()         # skill absent → no API call
    finally:
        await w.stop()


async def test_watcher_expiry_alert_fires_once_and_rearms():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    reg = _registry_with_skill(RECONNECT_MSG)
    w = EmailWatcher(reg, MagicMock(), notifier=notifier)
    assert await w._tick() == 0
    assert await w._tick() == 0
    notifier.notify.assert_awaited_once()     # alerted ONCE, not every 2 min
    # Healthy again → re-armed; next expiry alerts again.
    reg.call = AsyncMock(return_value={"status": "ok", "text": "[]"})
    await w._tick()
    reg.call = AsyncMock(return_value={"status": "ok", "text": RECONNECT_MSG})
    await w._tick()
    assert notifier.notify.await_count == 2


# ---------------------------------------------------------------------------
# Coordinator: voice "connect google"
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _silence_tts(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(
        speak=AsyncMock(), speak_sync=lambda *_: True))


def _coord():
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._skill_registry = None
    return c


def test_connect_phrases_are_system_control():
    cmd = MagicMock(); cmd.source = "voice"
    for phrase in ("connect google", "reconnect google", "set up gmail"):
        cmd.text = phrase
        assert _is_system_control_voice(cmd), phrase


async def test_connect_blocked_speaks_fix(monkeypatch):
    monkeypatch.setattr(gs, "setup_blocker", lambda: "Install the libs first.")
    res = await _coord()._handle_google_connect()
    assert res["action"] == "GOOGLE_CONNECT_BLOCKED"
    assert "libs" in res["reason"]


async def test_connect_starts_flow_when_unblocked(monkeypatch):
    monkeypatch.setattr(gs, "setup_blocker", lambda: None)
    c = _coord()
    c._google_connect_flow = AsyncMock()      # don't actually spawn a browser
    res = await c._handle_google_connect()
    assert res["action"] == "GOOGLE_CONNECT_STARTED"
    await asyncio.sleep(0)                    # let fire_and_log schedule it
    c._google_connect_flow.assert_called_once()


async def test_connect_flow_hot_starts_on_success(monkeypatch):
    import skills.google_setup as gsetup
    monkeypatch.setattr(gsetup, "run_auth_flow",
                        AsyncMock(return_value=(True, "Google connected.")))
    c = _coord()
    c._skill_registry = MagicMock()
    c._skill_registry.start_skill = AsyncMock(return_value=True)
    c._tts_speak = AsyncMock()
    await c._google_connect_flow()
    c._skill_registry.start_skill.assert_awaited_once_with("google_pim")
    assert "connected" in c._tts_speak.await_args.args[0].lower()


async def test_connect_flow_reports_failure(monkeypatch):
    import skills.google_setup as gsetup
    monkeypatch.setattr(gsetup, "run_auth_flow",
                        AsyncMock(return_value=(False, "sign-in timed out")))
    c = _coord()
    c._tts_speak = AsyncMock()
    await c._google_connect_flow()
    assert "timed out" in c._tts_speak.await_args.args[0]
