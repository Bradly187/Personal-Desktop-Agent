"""Command Executor

Maps the agent's action vocabulary onto the MCP server's desktop-control tools.

Accessibility verbs (11): CLICK, MOUSEDOWN, MOUSEUP, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, CLARIFY, SCREENSHOT
  MOUSEDOWN/MOUSEUP are executed synchronously in execute() and never reach _dispatch().
Dev-agent verbs (5):      WRITE_FILE, RUN_TERMINAL, EXPLAIN, SEARCH_WEB, READ_SCREEN

Imported directly by ipad_bridge.py so there's no HTTP round-trip;
the same tool functions that Claude calls are called here in-process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import logging
import os
import secrets
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from core.action_lifecycle import (
    ActionIdentity,
    ActionOutcome,
    ActionStatus,
    RegistryFullError,
    TrackedWorkRegistry,
)
from core.atomic_file import atomic_write_text
from core.execution_policy import ExecutionScope, load_writable_roots
from core.proxy_auth import (
    HEADER_INSTANCE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    body_sha256,
    canonical_json_bytes,
    capture_proxy_credentials,
    request_signature,
    sanitized_child_env,
)

if TYPE_CHECKING:
    from desktop.action_verifier import ActionVerifier
    from desktop.ui_automation import UIAutomationProvider

log = logging.getLogger(__name__)

# start_agent_wsl.sh supplies these only for module initialization. Capture the
# credential once, then remove it and the instance binding from os.environ so
# terminal commands, git hooks, and other descendants cannot inherit either.
_PROXY_URL: str | None = os.environ.get("WSL_ACTION_PROXY")
_PROXY_TOKEN, _PROXY_INSTANCE_ID = capture_proxy_credentials()


def _translate_wsl_path_for_windows_proxy(path: Path) -> str | None:
    """Pure syntax translation; callers must establish DrvFs provenance first."""
    parts = path.parts
    if (len(parts) < 3 or parts[0] not in ("/", "\\") or parts[1] != "mnt"
            or len(parts[2]) != 1 or not parts[2].isalpha()):
        return None
    tail = "\\".join(parts[3:])
    return f"{parts[2].upper()}:\\{tail}" if tail else f"{parts[2].upper()}:\\"


def _unescape_mountinfo_field(value: str) -> str:
    for escaped, plain in (("\\040", " "), ("\\011", "\t"),
                           ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(escaped, plain)
    return value


def _mountinfo_proves_drvfs(path: Path, mountinfo: str) -> bool:
    """Prove that path's /mnt/<drive> mount maps to that Windows drive."""
    parts = path.parts
    if (len(parts) < 3 or parts[0] != "/" or parts[1] != "mnt"
            or len(parts[2]) != 1 or not parts[2].isalpha()):
        return False
    mountpoint = f"/mnt/{parts[2].lower()}"
    drive = f"{parts[2].lower()}:"
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            continue
        actual_mountpoint = _unescape_mountinfo_field(left_fields[4]).lower()
        if actual_mountpoint != mountpoint:
            continue
        fs_type = right_fields[0].lower()
        source = _unescape_mountinfo_field(right_fields[1]).lower()
        options = ",".join((*left_fields[5:], *right_fields[2:])).lower()
        option_tokens = {
            token.strip()
            for token in options.replace(";", ",").split(",")
            if token.strip()
        }
        is_drvfs = fs_type == "drvfs" or (
            fs_type == "9p" and "aname=drvfs" in option_tokens
        )
        source_drive = source.rstrip("\\/") == drive
        option_drive = any(
            token.startswith("path=")
            and token.removeprefix("path=").rstrip("\\/") == drive
            for token in option_tokens
        )
        return is_drvfs and (source_drive or option_drive)
    return False


def _is_verified_wsl_drvfs_path(path: Path) -> bool:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return False
    return _mountinfo_proves_drvfs(path, mountinfo)


def _path_for_windows_proxy(
    path: Path,
    *,
    platform: str | None = None,
    mount_verifier: Callable[[Path], bool] | None = None,
) -> str | None:
    """Translate only native Windows paths or proven WSL DrvFs paths."""
    current = platform or os.name
    if current == "nt":
        return str(path)
    verifier = mount_verifier or _is_verified_wsl_drvfs_path
    if not verifier(path):
        return None
    return _translate_wsl_path_for_windows_proxy(path)


# ---------------------------------------------------------------------------
# Writable-root allowlist (M2) — bound WRITE_FILE / RUN_TERMINAL cwd
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_writable_roots() -> list[str]:
    """Compatibility adapter for existing MCP/search callers.

    Root ownership now lives in :class:`ExecutionScope`; the loader keeps the
    exact historical config and default-root contract.
    """
    return load_writable_roots()

# Only import pyautogui / win32gui tools when NOT running under the WSL proxy.
# In proxy mode these libraries are never called and importing them in WSL
# triggers Xlib/X11 warnings ("no xauthority", "install python3-tk") that
# fill the log with noise.
if not _PROXY_URL:
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))
    try:
        from tools import keyboard, mouse, screen, windows
    except Exception:
        keyboard = mouse = screen = windows = None
else:
    keyboard = mouse = screen = windows = None

# Lazy singleton — UIAutomation COM init is expensive; reuse across calls.
_ui_provider: "UIAutomationProvider | None" = None
_action_verifier: "ActionVerifier | None" = None

# Magnetic-click ("area cursor") radius in pixels: a tilt-tap clicks the nearest
# clickable target within this distance of the cursor instead of the exact pixel,
# so coarse tilt positioning is enough. Env-overridable for live tuning without a
# restart-edit; 0 (or negative) disables snapping → click lands at the cursor.
try:
    _SNAP_RADIUS_PX = int(os.environ.get("DA_SNAP_RADIUS_PX", "300"))
except ValueError:
    _SNAP_RADIUS_PX = 200


# ---------------------------------------------------------------------------
# Dedicated single-thread COM apartment for per-click UIAutomation work.
#
# UIA COM objects are apartment-bound: the UIAutomationProvider singleton caches
# a COM pointer (self._uia) that may ONLY be touched from the thread that created
# it. The per-click UIA paths run inside `asyncio.to_thread(self._dispatch, …)`,
# whose default ThreadPoolExecutor rotates worker threads across calls — so the
# pointer created on one worker would later be reused from another, a textbook
# cross-apartment access (RPC_E_WRONG_THREAD / E_POINTER). comtypes has been
# papering over it (no CoInitialize on those threads either), but it's a latent
# thread-identity race.
#
# Fix: funnel every provider COM call through this one CoInitialized(MTA) thread,
# so the cached pointer is created and used from a single apartment for the whole
# process lifetime. Same confinement the background ClickableTargetCache already
# uses for its own provider (desktop/target_cache.py). The threadpool worker that
# runs _dispatch just blocks on the future — it never touches COM itself.
_uia_executor: "concurrent.futures.ThreadPoolExecutor | None" = None


def _com_thread_init() -> None:
    """ThreadPoolExecutor initializer: CoInitialize the dedicated UIA thread (MTA)."""
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("UIA COM thread CoInitializeEx failed: %s", exc)


def _get_uia_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _uia_executor
    if _uia_executor is None:
        _uia_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="uia-com",
            initializer=_com_thread_init,
        )
    return _uia_executor


def _run_on_com_thread(fn, *args):
    """Run a UIA COM callable on the dedicated apartment thread, blocking for the
    result. Returns None on any executor-level failure (the callables themselves
    are written to swallow their own COM errors and return None)."""
    try:
        return _get_uia_executor().submit(fn, *args).result()
    except Exception as exc:
        log.debug("UIA COM thread call failed: %s", exc)
        return None


def _uia_nearest_snap(x: int, y: int, radius: int) -> "tuple[int, int] | None":
    """Direct per-click UIA nearest-target walk. Runs on the COM apartment thread.

    Both the is_available() probe (which lazily creates the COM pointer) and the
    tree walk happen here, so the pointer is born and used on the same thread.
    """
    provider = _get_ui_provider()
    if not (provider and provider.is_available()):
        return None
    try:
        elem = provider.find_nearest_clickable(x, y, max_radius=radius)
    except Exception as exc:
        log.debug("magnetic snap lookup failed: %s", exc)
        return None
    if elem is None:
        return None
    cx, cy = elem.center()
    log.info(
        "Magnetic snap: cursor (%d,%d) → %r [%s] (%d,%d)",
        x, y, elem.name or "?", elem.role, cx, cy,
    )
    return cx, cy


def _uia_find_target(target: str) -> "tuple[int, int] | None":
    """Structured named-element lookup (Sprint 6). Runs on the COM apartment thread."""
    provider = _get_ui_provider()
    if not (provider and provider.is_available()):
        return None
    try:
        element = provider.find(target, timeout_s=0.3)
    except Exception as exc:
        log.debug("UIAutomation lookup failed: %s", exc)
        return None
    if element and element.is_enabled:
        cx, cy = element.center()
        log.info("UIAutomation resolved %r → (%d, %d) [%s]", target, cx, cy, element.role)
        return cx, cy
    return None


def _magnetic_snap(x: int, y: int) -> "tuple[int, int] | None":
    """Return the center of the nearest clickable target within the snap radius
    of (x, y), or None if snapping is disabled / unavailable / nothing is near.

    Prefers the background ClickableTargetCache (cheap, no COM call on this
    thread); falls back to a direct per-click UIA tree walk when the cache isn't
    running yet (e.g. first click before the first refresh).
    """
    if _SNAP_RADIUS_PX <= 0:
        return None

    # Fast path — read the background snapshot.
    try:
        from desktop.target_cache import get_target_cache
        cache = get_target_cache()
        if cache.is_running():
            tg = cache.nearest(x, y, _SNAP_RADIUS_PX)
            if tg is None:
                return None
            cx, cy = tg.center()
            log.info("Magnetic snap [cache]: (%d,%d) → %r [%s] (%d,%d)",
                     x, y, tg.name or "?", tg.role, cx, cy)
            return cx, cy
    except Exception as exc:
        log.debug("magnetic snap cache lookup failed: %s", exc)

    # Fallback — direct UIA walk, confined to the dedicated COM apartment thread
    # (see _run_on_com_thread). Fires rarely now that the cache serves the fast
    # path — only before the first cache refresh / when the cache is unavailable.
    return _run_on_com_thread(_uia_nearest_snap, x, y, _SNAP_RADIUS_PX)

def _get_action_verifier():
    global _action_verifier
    if _action_verifier is None:
        try:
            from desktop.action_verifier import ActionVerifier
            _action_verifier = ActionVerifier()
        except Exception as exc:
            log.debug("ActionVerifier unavailable: %s", exc)
    return _action_verifier

def _get_ui_provider():
    global _ui_provider
    if _ui_provider is None:
        try:
            from desktop.ui_automation import UIAutomationProvider
            _ui_provider = UIAutomationProvider()
        except Exception as exc:
            log.debug("UIAutomationProvider unavailable: %s", exc)
    return _ui_provider

# ---------------------------------------------------------------------------
# Known-app registry — bypass Win+S for apps that Win Search misfires on.
# Keys are lowercased voice aliases; value is the absolute exe path.
# Paths use os.path.expandvars so they resolve to the running user's profile
# rather than being baked to a single username (#12).
# ---------------------------------------------------------------------------


def _find_discord_exe() -> str:
    """Return the Discord exe path, resolving the current version via glob.

    Discord updates silently and changes its `app-<version>` subdirectory on
    every release. A pinned path goes stale after any update. Glob picks the
    lexicographically latest version folder (newest version first when sorted
    in reverse). Falls back to the env-var expanded canonical path if no match
    is found (e.g. Discord not installed).
    """
    import glob
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\Discord\app-*\Discord.exe")
    matches = sorted(glob.glob(pattern), reverse=True)  # newest version first
    if matches:
        return matches[0]
    return os.path.expandvars(r"%LOCALAPPDATA%\Discord\Discord.exe")


_KNOWN_APPS: dict[str, str] = {
    # VS Code
    "vs code":          os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "vscode":           os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "visual studio code": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "code":             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    # Terminal — "terminal" opens PowerShell (user preference); the literal
    # "windows terminal" still launches the Windows Terminal app (wt.exe).
    "terminal":         r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "powershell":       r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "power shell":      r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "windows terminal": os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"),
    # Chrome
    "chrome":           r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome":    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # Slack
    "slack":            os.path.expandvars(r"%LOCALAPPDATA%\slack\slack.exe"),
    # Discord — discovered at startup via glob so a version update doesn't break OPEN
    "discord":          _find_discord_exe(),
}

# Detached-launch registry for the OPEN verb. Launched GUI apps must OUTLIVE the
# agent (the user keeps them open), so we deliberately never kill them — but the
# old fire-and-forget `Popen([exe])` had two real defects this fixes: (1) the app
# shared the agent's process group, so a Ctrl-C / shutdown signal to the agent's
# console could tear down the app the user just opened; (2) the handle was dropped,
# so on POSIX an app that exits while the agent lives lingers as a zombie. We now
# launch in a NEW process group/session (detached from the agent's signals) and
# keep the handle, opportunistically poll()-reaping exited launches on each OPEN.
_launched_procs: list = []


def _launch_detached(argv: list[str]) -> "subprocess.Popen":
    """Launch `argv` detached from the agent's signal group and track the handle.

    New process group on Windows / new session on POSIX so an agent shutdown
    signal does not propagate to the user's freshly-opened app. Never auto-killed.
    """
    kwargs: dict[str, Any] = {"env": sanitized_child_env()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # Reap previously-launched apps that have since exited (avoid POSIX zombies).
    for p in _launched_procs[:]:
        try:
            if p.poll() is not None:
                _launched_procs.remove(p)
        except Exception:
            _launched_procs.remove(p)
    proc = subprocess.Popen(argv, **kwargs)
    _launched_procs.append(proc)
    return proc


def _open_target_window_present(target: str) -> bool:
    """Corroborate an OPEN whose perceptual diff was ~0% (EH-1 / F3).

    Launching an app that is already running — or one that steals focus without a
    pixel delta — legitimately produces no visible change, yet the app IS open,
    which is exactly what OPEN promises. Before the verify block flags OPEN as a
    failure (which makes EH-1 emit a corrective CLARIFY and drags the command-
    domain success rate below its SLO floor), check whether a top-level window now
    matches the target so a real launch isn't reported as a miss. Conservative by
    design: any plausible match suppresses the false failure rather than nagging.
    """
    import re as _re
    try:
        # mcp_server is already on sys.path from the module-level insert at the
        # top of this file (inside `if not _PROXY_URL:`). No insert needed here.
        from tools import windows as win_tools
    except Exception:
        return False
    # Significant words from the target — drop short tokens ("vs", "my", "the").
    words = [w for w in _re.findall(r"[a-z0-9]+", (target or "").lower()) if len(w) >= 3]
    if not words:
        return False
    try:
        titles = [str(w.get("title") or "").lower() for w in win_tools.list_windows()]
    except Exception:
        return False
    return any(word in title for title in titles for word in words)


# URL shortcuts — opened with the default browser via os.startfile.
_KNOWN_URLS: dict[str, str] = {
    "claude":           "https://claude.ai",
    "claude ai":        "https://claude.ai",
    "linkedin":         "https://linkedin.com",
    "github":           "https://github.com",
    # Gmail / Google web apps — "open gmail" launches the website in the default
    # browser. This is a plain desktop action and needs NO OAuth / email skill;
    # the google_pim skill is only for reading/summarising/sending via the API.
    "gmail":            "https://mail.google.com",
    "google mail":      "https://mail.google.com",
    "email":            "https://mail.google.com",
    "my email":         "https://mail.google.com",
    "inbox":            "https://mail.google.com",
    "google":           "https://www.google.com",
    "google calendar":  "https://calendar.google.com",
    "calendar":         "https://calendar.google.com",
    "google drive":     "https://drive.google.com",
    "drive":            "https://drive.google.com",
    "youtube":          "https://youtube.com",
    "gemini":           "https://gemini.google.com",
}


# ---------------------------------------------------------------------------
# Amazon Polly TTS — spoken clarification for cloud-routed commands
# ---------------------------------------------------------------------------

_POLLY_VOICE = "Danielle"         # en-US Generative female; also supports Long-form
_POLLY_SAMPLE_RATE = 16_000       # 16 kHz PCM matches sounddevice default input rate
_POLLY_MAX_CHARS = 3_000          # Polly hard limit for standard text input
_POLLY_TIMEOUT_S = 5              # boto3 connect + read timeout

# Module-level singleton: boto3 clients are thread-safe and reusable; recreating
# one on every TTS call pays a TCP connection setup for each spoken clarification.
# Reset to None if credentials change (not expected at runtime) or on ImportError.
_POLLY_CLIENT = None  # boto3.client("polly") instance


def _polly_speak(message: str) -> bool:
    """Speak a clarification message via Amazon Polly Neural TTS.

    Called from _dispatch() which already runs in asyncio.to_thread, so
    this function is safe to block.

    Returns True if audio played successfully; False on any error
    (missing credentials, network timeout, sounddevice failure, etc.).
    All exceptions are caught internally — never raises.
    """
    global _POLLY_CLIENT

    if not message:
        return False

    if len(message) > _POLLY_MAX_CHARS:
        message = message[:_POLLY_MAX_CHARS]

    try:
        import boto3
        from botocore.config import Config
        import numpy as np
        import sounddevice as sd
        import threading as _threading
    except ImportError as exc:
        log.debug("Polly TTS: dependency missing (%s) — install boto3, numpy, sounddevice", exc)
        return False

    try:
        # Reuse the cached client; create on first call (#F).
        if _POLLY_CLIENT is None:
            cfg = Config(connect_timeout=_POLLY_TIMEOUT_S, read_timeout=_POLLY_TIMEOUT_S)
            _POLLY_CLIENT = boto3.client("polly", region_name="us-east-1", config=cfg)
        polly = _POLLY_CLIENT

        resp = polly.synthesize_speech(
            Text=message,
            OutputFormat="pcm",
            SampleRate=str(_POLLY_SAMPLE_RATE),
            VoiceId=_POLLY_VOICE,
            Engine="neural",
            LanguageCode="en-US",
        )

        audio_bytes: bytes = resp["AudioStream"].read()
        if not audio_bytes:
            log.warning("Polly TTS: empty audio stream returned")
            return False

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        duration_s = len(audio) / _POLLY_SAMPLE_RATE
        # Allow the audio duration plus a small network/device buffer for the
        # timeout. sd.wait() blocks until playback finishes naturally; we join
        # the wait-thread with this deadline to guard against a wedged audio
        # driver without reverting to a 50ms poll loop (#8).
        timeout_s = duration_s + _POLLY_TIMEOUT_S

        sd.play(audio, samplerate=_POLLY_SAMPLE_RATE)
        # Run sd.wait() on a daemon thread so we can join() with a timeout.
        # If the join times out, stop() the stream and return False.
        _wait_done = _threading.Event()

        def _wait_worker():
            sd.wait()
            _wait_done.set()

        _t = _threading.Thread(target=_wait_worker, daemon=True)
        _t.start()
        if not _wait_done.wait(timeout=timeout_s):
            sd.stop()
            log.warning("Polly TTS: playback timed out after %.1fs", timeout_s)
            return False

        log.info("Polly TTS: spoke %d chars (%.1fs) via voice=%s",
                 len(message), duration_s, _POLLY_VOICE)
        return True

    except Exception as exc:
        log.warning("Polly TTS failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Universal DTO (mirrors the spec's Command dataclass)
# ---------------------------------------------------------------------------

@dataclass
class Command:
    text: str
    action: str  # accessibility: CLICK|SCROLL|TYPE|OPEN|CLOSE|HOTKEY|DICTATE|CLARIFY|SCREENSHOT
                 # dev-agent:    WRITE_FILE|RUN_TERMINAL|EXPLAIN|SEARCH_WEB|READ_SCREEN
    source: str                          # touch | trackpad | sound_action | voice | ...
    whisper_logprob: float = 0.0
    gesture_confidence: float = 1.0
    session_context: list[str] = field(default_factory=list)
    gaze_coords: tuple[int, int] | None = None  # explicit click pixel coords (vision grounder, touch)
    params: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""                   # cross-layer trace id (set when DA_TRACE on); see monitoring/trace.py
    action_id: str = ""                  # unguessable idempotent lifecycle identity
    run_id: int | None = None
    step_num: int | None = None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

import hashlib
import json as _json

# Verbs for which we compute a SHA-256 idempotency key and skip re-execution
# when the exact same call already succeeded in this session (safe to deduplicate).
_IDEMPOTENT_VERBS: frozenset[str] = frozenset({"WRITE_FILE"})

# Verbs where a verified "no visible change" is a genuine failure worth a
# corrective CLARIFY (EH-1). SCROLL is deliberately excluded — scrolling to the
# end of a page legitimately produces no change and must not nag the user.
_VERIFY_FAIL_VERBS: frozenset[str] = frozenset({"CLICK", "OPEN", "CLOSE"})

# Operations proven to have no externally visible side effect. SEARCH_WEB is
# intentionally absent: opening a browser is an action, not a read.
_SIDE_EFFECT_FREE_VERBS: frozenset[str] = frozenset({"READ_SCREEN", "EXPLAIN"})
_RECONCILIATION_GRACE_S = 2.0
_UNKNOWN_REGISTRY_LIMIT = 32
_UNKNOWN_REGISTRY_TTL_S = 300.0


class TargetResolutionError(Exception):
    """A named click target could not be resolved by any locator tier.

    Raised by ``_resolve_coords`` (strict mode) instead of silently falling back
    to the cursor position, so the coordinator can emit an honest CLARIFY rather
    than clicking wherever the cursor happens to be (EH-1 / E2). The message is
    the target text that could not be found.
    """


class CommandExecutor:
    """Translates a Command into one or more desktop tool calls."""

    def __init__(
        self,
        execution_scope: ExecutionScope | None = None,
        *,
        action_registry: TrackedWorkRegistry | None = None,
        containment_policy=None,
    ) -> None:
        self._agent_db = None  # AgentDB — set via set_agent_db(); optional
        self._audit_log = None  # AuditLog — set via set_audit_log(); optional
        self._execution_scope = execution_scope or ExecutionScope()
        if containment_policy is None:
            from inference.sandbox import get_containment_policy
            containment_policy = get_containment_policy()
        self._containment_policy = containment_policy
        self._action_registry = action_registry or TrackedWorkRegistry(
            limit=_UNKNOWN_REGISTRY_LIMIT, ttl_s=_UNKNOWN_REGISTRY_TTL_S
        )

    # Private compatibility adapter: older tests and a few callers assigned this
    # list directly.  Keep the surface, but never restore duplicate ownership.
    @property
    def _writable_roots(self) -> list[str]:
        return [str(root) for root in self._execution_scope.roots()]

    @_writable_roots.setter
    def _writable_roots(self, roots: list[str]) -> None:
        self._execution_scope._replace_roots_for_compat(roots)

    def add_writable_root(self, path: str) -> bool:
        """Human-confirmed compatibility entry point for adding a real root."""
        return self._execution_scope.add_root(path, human_confirmed=True).allowed

    def set_active_root(self, path: str) -> bool:
        """Human-confirmed compatibility entry point for activating a root."""
        return self._execution_scope.set_active_root(
            path, human_confirmed=True
        ).allowed

    @property
    def active_root(self) -> str | None:
        active = self._execution_scope.active_root()
        return str(active) if active is not None else None

    @property
    def writable_roots(self) -> list[str]:
        """Read-only view of the current allowlist (for the chat picker, R1.5)."""
        return [str(root) for root in self._execution_scope.roots()]

    def set_agent_db(self, db) -> None:
        """Wire AgentDB for per-call idempotency tracking and timeout config."""
        self._agent_db = db

    def set_audit_log(self, audit_log) -> None:
        """Wire AuditLog so command execution failures are recorded in the tamper-evident trail."""
        self._audit_log = audit_log

    async def _tool_timeout_ms(self, action: str) -> int:
        """Return the configured timeout for action (ms). Falls back to 30 000."""
        if self._agent_db and getattr(self._agent_db, "available", False):
            try:
                timeout_ms, _ = await self._agent_db.logs.get_tool_timeout(action.lower())
                return timeout_ms
            except Exception:
                pass
        _defaults = {
            "WRITE_FILE": 15_000, "RUN_TERMINAL": 30_000, "SCREENSHOT": 5_000,
            "READ_SCREEN": 5_000, "CLICK": 5_000, "TYPE": 10_000,
        }
        return _defaults.get(action.upper(), 30_000)

    @staticmethod
    def _make_idempotency_key(action: str, params: dict) -> str:
        canon = _json.dumps({"action": action.upper(), "params": params},
                            sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    async def _signed_proxy_post(
        self, session, path: str, payload: dict[str, Any], *, timeout_s: float
    ) -> tuple[int, dict]:
        """POST canonical JSON authenticated for this proxy instance."""
        import aiohttp

        body = canonical_json_bytes(payload)
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = request_signature(
            _PROXY_TOKEN,
            method="POST",
            path=path,
            body_hash=body_sha256(body),
            timestamp=timestamp,
            nonce=nonce,
            instance_id=_PROXY_INSTANCE_ID,
        )
        headers = {
            "Content-Type": "application/json",
            HEADER_INSTANCE: _PROXY_INSTANCE_ID,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: signature,
        }
        async with session.post(
            f"{_PROXY_URL}{path}",
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as response:
            return response.status, await response.json()

    @staticmethod
    def _proxy_identity_payload(cmd: Command) -> dict[str, Any]:
        return {
            "version": 2,
            "action_id": cmd.action_id,
            "trace_id": cmd.trace_id,
            "run_id": cmd.run_id,
            "step_num": cmd.step_num,
            "action": cmd.action.upper(),
        }

    @staticmethod
    def _validate_proxy_outcome(
        data: dict[str, Any], identity: ActionIdentity
    ) -> ActionOutcome:
        if data.get("version") != 2:
            raise ValueError("proxy response has unsupported lifecycle version")
        for key, expected in (
            ("action_id", identity.action_id),
            ("action", identity.action),
            ("trace_id", identity.trace_id),
            ("run_id", identity.run_id),
            ("step_num", identity.step_num),
        ):
            if data.get(key) != expected:
                raise ValueError(f"proxy response identity mismatch: {key}")
        if data.get("lifecycle_status") not in {
            status.value for status in ActionStatus
        }:
            raise ValueError("proxy response has invalid lifecycle status")
        return ActionOutcome.from_dict(data, identity=identity)

    def _observe_proxy_containment(
        self, cmd: Command, outcome: ActionOutcome
    ) -> None:
        """Mirror a validated receiver report into this process's status cache."""
        if cmd.action.upper() != "RUN_TERMINAL" or not isinstance(outcome.result, dict):
            return
        payload = outcome.result.get("containment")
        if payload is None:
            return
        try:
            from inference.sandbox import (
                ContainmentReport,
                work_trust_from_source,
            )

            report = ContainmentReport.from_dict(payload)
            self._containment_policy.observe(
                report, work_trust_from_source(cmd.source)
            )
        except (TypeError, ValueError) as exc:
            log.warning("Ignoring invalid proxy containment report: %s", exc)

    async def _proxy_execute(
        self,
        cmd: Command,
        preauthorization: dict[str, str | int] | None,
        *,
        timeout_s: float = 30.0,
    ) -> dict:
        """Execute once through the signed proxy and reconcile transport loss."""
        import aiohttp

        if not _PROXY_TOKEN or not _PROXY_INSTANCE_ID:
            return ActionOutcome(
                ActionStatus.FAILED, cmd.action.upper(), cmd.trace_id,
                cmd.run_id, cmd.step_num, cmd.action_id,
                error="proxy authentication unavailable",
            ).to_dict()
        identity_obj = ActionIdentity(
            cmd.action_id, cmd.action.upper(), cmd.trace_id, cmd.run_id, cmd.step_num
        )
        identity_payload = self._proxy_identity_payload(cmd)
        payload = {
            **identity_payload,
            "text": cmd.text,
            "source": cmd.source,
            "params": cmd.params,
            "whisper_logprob": cmd.whisper_logprob,
            "gesture_confidence": cmd.gesture_confidence,
            "gaze_coords": list(cmd.gaze_coords) if cmd.gaze_coords else None,
            "session_context": cmd.session_context,
            "wait_timeout_s": timeout_s,
        }
        if preauthorization is not None:
            payload["_execution_scope"] = preauthorization

        transport_error: Exception | None = None
        async with aiohttp.ClientSession() as session:
            try:
                status, result = await self._signed_proxy_post(
                    session, "/execute", payload, timeout_s=timeout_s
                )
                if status in {400, 401, 403, 409, 422, 503}:
                    return ActionOutcome(
                        ActionStatus.FAILED,
                        identity_obj.action,
                        identity_obj.trace_id,
                        identity_obj.run_id,
                        identity_obj.step_num,
                        identity_obj.action_id,
                        error=str(result.get("error") or f"proxy rejected action ({status})"),
                        reconciliation=f"signed execute rejected with HTTP {status}",
                    ).to_dict()
                if 200 <= status < 300:
                    observed = self._validate_proxy_outcome(result, identity_obj)
                    self._observe_proxy_containment(cmd, observed)
                    if observed.status is not ActionStatus.OUTCOME_UNKNOWN:
                        return observed.to_dict()
                else:
                    transport_error = RuntimeError(f"proxy execute returned HTTP {status}")
            except Exception as exc:
                # Once a signed body may have left this process, execution is
                # ambiguous. Never collapse a disconnect/timeout into failure.
                transport_error = exc
                log.warning(
                    "Windows action proxy execute response unavailable for %s: %s",
                    cmd.action_id, exc,
                )

            # Continue signed reconciliation for the registry TTL, not only the
            # caller's grace. The owning local registry remains the durable
            # in-process bridge used by interrupted-run resume checks.
            deadline = time.monotonic() + _UNKNOWN_REGISTRY_TTL_S
            while time.monotonic() < deadline:
                try:
                    status, result = await self._signed_proxy_post(
                        session,
                        "/result",
                        identity_payload,
                        timeout_s=min(2.0, max(0.1, deadline - time.monotonic())),
                    )
                    # 404 is not terminal evidence: a separately connected
                    # execute request may still be in transit. Only a matching,
                    # versioned success envelope can settle the action.
                    if 200 <= status < 300:
                        observed = self._validate_proxy_outcome(result, identity_obj)
                        self._observe_proxy_containment(cmd, observed)
                        if observed.status is not ActionStatus.OUTCOME_UNKNOWN:
                            return observed.to_dict()
                except Exception as exc:
                    transport_error = exc
                await asyncio.sleep(0.1)

        return ActionOutcome(
            ActionStatus.OUTCOME_UNKNOWN,
            cmd.action.upper(),
            cmd.trace_id,
            cmd.run_id,
            cmd.step_num,
            cmd.action_id,
            error=f"proxy response unavailable; outcome is unknown: {transport_error}",
            reconciliation="signed result query did not produce terminal evidence",
        ).to_dict()

    def _proxy_scope_preflight(
        self, cmd: Command
    ) -> tuple[Command | None, dict[str, str | int] | None, dict | None]:
        """Authorize paths and create a narrowing receiver authorization."""
        action = cmd.action.upper()
        params = dict(cmd.params)
        decision = None
        field = ""
        raw_value = None
        if action == "WRITE_FILE":
            field = "path"
            raw_value = params.get(field, "")
            decision = self._execution_scope.authorize_path(
                raw_value, operation="WRITE_FILE"
            )
        elif action == "RUN_TERMINAL":
            field = "cwd"
            raw_value = params.get(field) or None
            decision = self._execution_scope.authorize_cwd(raw_value)

        if decision is None:
            return replace(cmd, params=params), None, None
        if (not decision.allowed or decision.canonical_path is None
                or decision.root is None):
            return None, None, {
                "status": "error",
                "error": f"{action} denied: {field} {raw_value} is outside the writable-root allowlist",
            }

        proxy_path = _path_for_windows_proxy(decision.canonical_path)
        proxy_root = _path_for_windows_proxy(decision.root)
        if proxy_path is None or proxy_root is None:
            return None, None, {
                "status": "error",
                "error": f"{action} denied: {field} {raw_value} has no safe Windows proxy path",
            }
        params[field] = proxy_path
        preauthorization: dict[str, str | int] = {
            "version": 1,
            "action": action,
            "root": proxy_root,
            "canonical_path": proxy_path,
        }
        return replace(cmd, params=params), preauthorization, None

    async def execute(self, cmd: Command) -> dict:
        return await self._execute_impl(cmd, registry_managed=False)

    async def execute_with_registry(
        self, cmd: Command, registry: TrackedWorkRegistry
    ) -> dict:
        """Execute using a caller-owned registry (plan composition boundary)."""
        return await self._execute_impl(
            cmd, registry_managed=False, registry_override=registry
        )

    async def execute_registry_managed(self, cmd: Command) -> dict:
        """Receiver-only entry point when an outer registry owns ``cmd``."""
        return await self._execute_impl(cmd, registry_managed=True)

    async def _execute_impl(
        self,
        cmd: Command,
        *,
        registry_managed: bool,
        registry_override: TrackedWorkRegistry | None = None,
    ) -> dict:
        """Execute through the typed lifecycle and adapt at the dict boundary."""
        action = cmd.action.upper()
        if not cmd.action_id:
            cmd = replace(cmd, action_id=secrets.token_urlsafe(32))
        identity = ActionIdentity(
            cmd.action_id, action, cmd.trace_id, cmd.run_id, cmd.step_num
        )
        timeout_ms = await self._tool_timeout_ms(action)
        timeout_s = timeout_ms / 1000

        # WSL mode is a private, instance-bound action proxy contract. The
        # receiver owns the retained task and idempotency record.
        if _PROXY_URL:
            proxied_cmd, preauthorization, denial = await asyncio.to_thread(
                self._proxy_scope_preflight, cmd
            )
            if denial is not None:
                # Preserve the established Phase 1-3 boundary shape: transport
                # succeeded, while the nested action result records denial.
                return {
                    "status": "ok",
                    "lifecycle_status": ActionStatus.FAILED.value,
                    "action": action,
                    "action_id": cmd.action_id,
                    "trace_id": cmd.trace_id,
                    "run_id": cmd.run_id,
                    "step_num": cmd.step_num,
                    "result": denial,
                }
            assert proxied_cmd is not None
            registry = (
                registry_override
                if registry_override is not None
                else self._action_registry
            )
            try:
                outcome = await registry.execute(
                    identity,
                    lambda: self._proxy_execute(
                        proxied_cmd, preauthorization, timeout_s=timeout_s
                    ),
                    timeout_s=timeout_s,
                    destructive=action not in _SIDE_EFFECT_FREE_VERBS,
                    reconciliation_grace_s=_RECONCILIATION_GRACE_S,
                )
            except RegistryFullError as exc:
                outcome = ActionOutcome(
                    ActionStatus.FAILED,
                    action,
                    cmd.trace_id,
                    cmd.run_id,
                    cmd.step_num,
                    cmd.action_id,
                    error=f"action denied before proxy execution: {exc}",
                )
            return outcome.to_dict()

        # MOUSEDOWN/MOUSEUP stay synchronous because drag timing must not compete
        # with trackpad worker threads. They have no abandoned-worker boundary.
        if action in ("MOUSEDOWN", "MOUSEUP"):
            try:
                import pyautogui
                x, y = pyautogui.position()
                if action == "MOUSEDOWN":
                    pyautogui.mouseDown(x, y, button="left", _pause=False)
                    result = {"mousedown": True, "x": x, "y": y}
                else:
                    pyautogui.mouseUp(x, y, button="left", _pause=False)
                    result = {"mouseup": True, "x": x, "y": y}
                return ActionOutcome(
                    ActionStatus.COMPLETED, action, cmd.trace_id, cmd.run_id,
                    cmd.step_num, cmd.action_id, result=result,
                ).to_dict()
            except Exception as exc:
                return ActionOutcome(
                    ActionStatus.FAILED, action, cmd.trace_id, cmd.run_id,
                    cmd.step_num, cmd.action_id, error=str(exc),
                ).to_dict()

        idem_key: str | None = None
        if action in _IDEMPOTENT_VERBS:
            idem_key = self._make_idempotency_key(action, cmd.params)
            if self._agent_db and getattr(self._agent_db, "available", False):
                try:
                    cached = await self._agent_db.logs.get_tool_call_by_idempotency(idem_key)
                    if cached:
                        import json as _j
                        return {
                            **ActionOutcome(
                                ActionStatus.COMPLETED, action, cmd.trace_id,
                                cmd.run_id, cmd.step_num, cmd.action_id,
                                result=_j.loads(cached.get("result_json") or "{}"),
                            ).to_dict(),
                            "idempotent": True,
                        }
                except Exception as exc:
                    log.debug("CommandExecutor: idempotency check failed: %s", exc)

        from desktop.action_verifier import VERIFIABLE_VERBS
        verifier = _get_action_verifier()
        pre_b64 = ""
        if verifier and action in VERIFIABLE_VERBS:
            pre_b64 = await asyncio.to_thread(verifier.snapshot)

        # Ensure the process owner's timeout fires early enough to terminate and
        # verify the tree before this caller budget expires.
        dispatch_cmd = cmd
        if action == "RUN_TERMINAL":
            params = dict(cmd.params)
            params["_process_timeout_s"] = max(0.1, timeout_s - 7.5)
            dispatch_cmd = replace(cmd, params=params)

        async def work():
            try:
                return await asyncio.to_thread(self._dispatch, action, dispatch_cmd)
            except TargetResolutionError as exc:
                return {"_resolve_miss": str(exc)}

        async def reconcile(outcome: ActionOutcome) -> str | None:
            if action == "WRITE_FILE":
                raw_path = dispatch_cmd.params.get("path", "")
                decision = self._execution_scope.authorize_path(
                    raw_path, operation="WRITE_FILE"
                )
                if not decision.allowed or decision.canonical_path is None:
                    return "file evidence unavailable: path no longer authorized"
                expected = str(dispatch_cmd.params.get("content", dispatch_cmd.text))

                def observe_file() -> str:
                    path = decision.canonical_path
                    if not path.exists():
                        return "file evidence: target absent"
                    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
                    return (
                        "file evidence: intended hash present"
                        if actual_hash == expected_hash
                        else "file evidence: intended hash not present"
                    )

                return await asyncio.to_thread(observe_file)
            if action == "RUN_TERMINAL":
                if outcome.status is ActionStatus.COMPLETED:
                    rc = (outcome.result or {}).get("returncode")
                    return f"process evidence: exited with returncode {rc}"
                return outcome.reconciliation
            if verifier and pre_b64 and action in VERIFIABLE_VERBS:
                observed = await asyncio.to_thread(verifier.verify, pre_b64, action)
                if observed.success:
                    return f"desktop evidence: visible change ({observed.diff_pct:.1f}%)"
                if action == "OPEN":
                    target = dispatch_cmd.params.get("target", dispatch_cmd.text)
                    if await asyncio.to_thread(_open_target_window_present, target):
                        return "desktop evidence: target window present"
                return "desktop evidence unavailable"
            return None

        t_start = time.monotonic()
        try:
            if registry_managed:
                # The authenticated proxy registry owns this exact action ID and
                # shields this coroutine; do not recursively register it.
                raw_result = await work()
                outcome = ActionOutcome(
                    ActionStatus.COMPLETED, action, cmd.trace_id, cmd.run_id,
                    cmd.step_num, cmd.action_id, result=raw_result,
                )
            else:
                registry = (
                    registry_override
                    if registry_override is not None
                    else self._action_registry
                )
                outcome = await registry.execute(
                    identity,
                    work,
                    timeout_s=timeout_s,
                    destructive=action not in _SIDE_EFFECT_FREE_VERBS,
                    reconciliation_grace_s=_RECONCILIATION_GRACE_S,
                    reconciler=reconcile,
                )
        except RegistryFullError as exc:
            outcome = ActionOutcome(
                ActionStatus.FAILED, action, cmd.trace_id, cmd.run_id,
                cmd.step_num, cmd.action_id,
                error=f"action denied before execution: {exc}",
            )
        latency_ms = (time.monotonic() - t_start) * 1000

        if outcome.status is not ActionStatus.COMPLETED:
            event = (
                "command_execution_unknown"
                if outcome.status is ActionStatus.OUTCOME_UNKNOWN
                else "command_execution_failed"
            )
            log.warning("CommandExecutor: %s %s: %s", action, outcome.status.value, outcome.error)
            if self._audit_log and self._audit_log.available:
                try:
                    await self._audit_log.log(
                        event,
                        severity="warning",
                        tool=action,
                        detail=(outcome.error or outcome.status.value)[:400],
                        params={
                            "action": action,
                            "source": cmd.source,
                            "trace_id": cmd.trace_id,
                            "action_id": cmd.action_id,
                        },
                        outcome=(
                            ActionStatus.OUTCOME_UNKNOWN.value
                            if outcome.status is ActionStatus.OUTCOME_UNKNOWN
                            else "timeout"
                            if "timeout" in (outcome.error or "").lower()
                            else "exception"
                        ),
                    )
                except Exception as exc:
                    log.debug("CommandExecutor: audit log write failed: %s", exc)
            return outcome.to_dict()

        result = outcome.result
        if isinstance(result, dict) and "_resolve_miss" in result:
            return {
                "status": "resolve_miss",
                "lifecycle_status": ActionStatus.FAILED.value,
                "action": action,
                "action_id": cmd.action_id,
                "trace_id": cmd.trace_id,
                "target": result["_resolve_miss"],
            }
        log.info("Executed %s [source=%s]: %s", action, cmd.source, result)

        verify_result = None
        if verifier and pre_b64:
            vr = await asyncio.to_thread(verifier.verify, pre_b64, action)
            verify_result = {
                "success": vr.success,
                "diff_pct": vr.diff_pct,
                "reason": vr.reason,
                "elapsed_ms": vr.elapsed_ms,
            }
            if not vr.success:
                log.warning(
                    "ActionVerifier: %s — no visible change (diff=%.1f%%, %dms)",
                    action, vr.diff_pct, vr.elapsed_ms,
                )

        if self._agent_db and getattr(self._agent_db, "available", False):
            try:
                import json as _j
                await self._agent_db.logs.insert_tool_call(
                    action.lower(),
                    idempotency_key=idem_key,
                    args_json=_j.dumps(cmd.params, separators=(",", ":")) if cmd.params else None,
                    result_json=_j.dumps(result, separators=(",", ":")),
                    success=True,
                    latency_ms=round(latency_ms, 1),
                    timeout_ms=timeout_ms,
                    status="completed",
                )
            except Exception as exc:
                log.debug("CommandExecutor: insert_tool_call failed: %s", exc)

        verify_failed = bool(
            verify_result
            and not verify_result["success"]
            and verify_result["reason"] == "no_change"
            and action in _VERIFY_FAIL_VERBS
        )
        if verify_failed and action == "OPEN":
            target_text = (result or {}).get("opened") or cmd.text or ""
            if await asyncio.to_thread(_open_target_window_present, target_text):
                verify_failed = False
                verify_result["success"] = True
                verify_result["reason"] = "window_present"
        response = outcome.to_dict()
        response["status"] = "verify_failed" if verify_failed else "ok"
        if verify_result:
            response["verification"] = verify_result
        return response

    def _dispatch(self, action: str, cmd: Command) -> dict:
        p = cmd.params

        # ------------------------------------------------------------------ #
        # CLICK — left-click at provided coords, explicit click coords, or screen centre
        # ------------------------------------------------------------------ #
        if action == "CLICK":
            x, y, resolved_by = self._resolve_coords(cmd, strict_target=True)
            btn = p.get("button", "left")
            res = mouse.mouse_click(x, y, button=btn)
            if isinstance(res, dict):
                res["resolved_by"] = resolved_by
            return res

        # ------------------------------------------------------------------ #
        # SCROLL
        # ------------------------------------------------------------------ #
        if action == "SCROLL":
            x, y, _ = self._resolve_coords(cmd)
            direction = p.get("direction", "down")
            clicks = int(p.get("amount", 3))
            return mouse.mouse_scroll(x, y, direction=direction, clicks=clicks)

        # ------------------------------------------------------------------ #
        # TYPE — type a literal string
        # ------------------------------------------------------------------ #
        if action == "TYPE":
            text = p.get("text", cmd.text)
            return keyboard.keyboard_type(text)

        # ------------------------------------------------------------------ #
        # OPEN — known-app shortcut → subprocess; unknown → Win+S search
        # ------------------------------------------------------------------ #
        if action == "OPEN":
            target = p.get("target", cmd.text)
            key = target.lower().strip()
            # 1. Known exe path
            exe = _KNOWN_APPS.get(key)
            if exe and Path(exe).exists():
                _launch_detached([exe])
                return {"status": "ok", "opened": target, "method": "direct"}
            # 2. Known URL
            url = _KNOWN_URLS.get(key)
            if url:
                import os
                os.startfile(url)
                return {"status": "ok", "opened": target, "method": "url"}
            # 3. Win+S search fallback
            keyboard.keyboard_hotkey("win", "s")
            time.sleep(0.4)
            keyboard.keyboard_type(target)
            time.sleep(0.5)
            keyboard.keyboard_press("enter")
            return {"status": "ok", "opened": target, "method": "search"}

        # ------------------------------------------------------------------ #
        # CLOSE — Alt+F4 on the active window
        # ------------------------------------------------------------------ #
        if action == "CLOSE":
            return keyboard.keyboard_hotkey("alt", "f4")

        # ------------------------------------------------------------------ #
        # HOTKEY — arbitrary key combo, e.g. ["ctrl","c"]
        # ------------------------------------------------------------------ #
        if action == "HOTKEY":
            keys = p.get("keys", [])
            if not keys:
                raise ValueError("HOTKEY action requires 'keys' param")
            # Support key hold/release for select mode
            state = p.get("state", "")
            if state == "down":
                key = keys[0] if isinstance(keys, list) else keys
                return keyboard.keyboard_key_down(key)
            elif state == "up":
                key = keys[0] if isinstance(keys, list) else keys
                return keyboard.keyboard_key_up(key)
            return keyboard.keyboard_hotkey(*keys)

        # ------------------------------------------------------------------ #
        # DICTATE — paste transcribed text via clipboard (full unicode support)
        # ------------------------------------------------------------------ #
        if action == "DICTATE":
            return keyboard.keyboard_paste(cmd.text)

        # ------------------------------------------------------------------ #
        # SCREENSHOT — capture the active window (or explicit region)
        # ------------------------------------------------------------------ #
        if action == "SCREENSHOT":
            region = p.get("region")  # optional {"left","top","width","height"}
            if not region:
                # Default: capture the active window
                from tools import windows as win_tools
                win = win_tools.get_active_window()
                if win.get("width") and win.get("height"):
                    region = {
                        "left": win["x"],
                        "top": win["y"],
                        "width": win["width"],
                        "height": win["height"],
                    }
            result = screen.screenshot(region=region)
            # Copy to Windows clipboard so user can Ctrl+V
            try:
                import base64
                from PIL import Image
                import win32clipboard
                img_bytes = base64.b64decode(result["image_base64"])
                img = Image.open(io.BytesIO(img_bytes))
                # Convert to BMP for clipboard (strip 14-byte BMP file header)
                bmp_buf = io.BytesIO()
                img.save(bmp_buf, format="BMP")
                bmp_data = bmp_buf.getvalue()[14:]  # skip BITMAPFILEHEADER
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp_data)
                win32clipboard.CloseClipboard()
            except Exception as exc:
                log.warning("Failed to copy screenshot to clipboard: %s", exc)
            return result

        # ------------------------------------------------------------------ #
        # CLARIFY — speak via Polly Bidirectional Streaming TTS (always)
        # ------------------------------------------------------------------ #
        if action == "CLARIFY":
            message = p.get("message", "Unclear command")
            spoken = False
            try:
                from tts.polly_stream import get_client as _get_tts
                spoken = _get_tts().speak_sync(message)
            except Exception as _tts_exc:
                log.debug("TTS speak failed, falling back to legacy Polly: %s", _tts_exc)
                if p.get("route") == "cloud":
                    spoken = _polly_speak(message)
            return {"status": "ok", "clarify": True, "message": message, "spoken": spoken}

        # ================================================================== #
        # Dev-agent extended verbs
        # ================================================================== #

        # ------------------------------------------------------------------ #
        # WRITE_FILE — write content to a file on disk
        # params: {path: str, content: str}
        # ------------------------------------------------------------------ #
        if action == "WRITE_FILE":
            raw_path = p.get("path", "")
            content = p.get("content", cmd.text)
            decision = self._execution_scope.authorize_path(
                raw_path, operation="WRITE_FILE"
            )
            if not decision.allowed or decision.canonical_path is None:
                log.warning("WRITE_FILE denied: %s", decision.reason)
                return {"status": "error",
                        "error": f"WRITE_FILE denied: {raw_path} is outside the writable-root allowlist"}
            path = decision.canonical_path
            byte_count = atomic_write_text(path, str(content))
            return {"written": str(path), "bytes": byte_count}

        # ------------------------------------------------------------------ #
        # RUN_TERMINAL — run a shell command and capture output
        # params: {command: str, cwd: str | None}
        # ------------------------------------------------------------------ #
        if action == "RUN_TERMINAL":
            command = p.get("command", cmd.text)
            raw_cwd = p.get("cwd") or None
            decision = self._execution_scope.authorize_cwd(raw_cwd)
            if not decision.allowed or decision.canonical_path is None:
                log.warning("RUN_TERMINAL denied: %s", decision.reason)
                return {"status": "error",
                        "error": f"RUN_TERMINAL denied: cwd {raw_cwd} is outside the writable-root allowlist"}
            cwd = str(decision.canonical_path)
            # Slopsquatting guard (GAP-7): refuse a `pip install` of a package
            # that doesn't exist on PyPI (or via a redirected index) before it
            # runs. Mirrors DevAgent._run_terminal so BOTH RUN_TERMINAL paths are
            # covered. Fails open on a network error so offline dev isn't blocked.
            from core.goal_session import verify_pip_install
            ok, reason = verify_pip_install(command)
            if not ok:
                log.warning("RUN_TERMINAL denied: %s", reason)
                return {"status": "error", "error": f"RUN_TERMINAL denied: {reason}"}
            # Sandbox (mistake-containment): cwd-jail + caps. Network granted only
            # for curated package/VCS/fetch ops, or when the caller explicitly
            # passes allow_network in params (e.g. an approved network step).
            from inference.sandbox import (
                ContainmentDeniedError,
                command_needs_network,
                run_sandboxed,
                work_trust_from_source,
            )
            net = bool(p.get("allow_network")) or command_needs_network(command)
            process_timeout_s = float(p.get("_process_timeout_s", 60.0))
            try:
                result = run_sandboxed(
                    command,
                    project_dir=cwd,
                    timeout=process_timeout_s,
                    allow_network=net,
                    work_trust=work_trust_from_source(cmd.source),
                    containment_policy=self._containment_policy,
                )
            except ContainmentDeniedError as exc:
                return {
                    "status": "error",
                    "error": str(exc),
                    "sandboxed": False,
                    "containment": exc.report.to_dict(),
                    "network": net,
                }
            return {
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
                "sandboxed": result.sandboxed,
                "containment": (
                    getattr(result, "containment", None).to_dict()
                    if getattr(result, "containment", None)
                    else None
                ),
                "network": net,
            }

        # ------------------------------------------------------------------ #
        # EXPLAIN — return text to caller; no desktop action
        # params: {text: str}
        # ------------------------------------------------------------------ #
        if action == "EXPLAIN":
            return {"explanation": p.get("text", cmd.text)}

        # ------------------------------------------------------------------ #
        # SEARCH_WEB — open default browser with search query
        # params: {query: str}
        # ------------------------------------------------------------------ #
        if action == "SEARCH_WEB":
            from urllib.parse import urlencode
            query = p.get("query", cmd.text)
            url = "https://www.google.com/search?" + urlencode({"q": query})
            webbrowser.open(url)
            return {"status": "ok", "opened": url}

        # ------------------------------------------------------------------ #
        # READ_SCREEN — capture screenshot for vision model consumption
        # ------------------------------------------------------------------ #
        if action == "READ_SCREEN":
            region = p.get("region")
            return screen.screenshot(region=region)

        # ------------------------------------------------------------------ #
        # SNAP_WINDOW — move hwnd to snap zone rect (D7 flick-to-snap)
        # params: {zone: str, hwnd: int, rect: [l,t,r,b] (optional)}
        # ------------------------------------------------------------------ #
        if action == "SNAP_WINDOW":
            hwnd = int(p.get("hwnd", 0))
            rect = p.get("rect")
            zone = p.get("zone", "")
            if not hwnd:
                import win32gui as _wg
                hwnd = _wg.GetForegroundWindow()
            if rect:
                l, t, r, b = rect
                from desktop.snap_zones import apply_snap
                ok = apply_snap(hwnd, (l, t, r, b))
                return {"status": "ok" if ok else "error", "zone": zone, "hwnd": hwnd}
            # If no rect supplied, snap zones are resolved by FlickEngine before
            # this point — this branch handles manual calls with explicit rect.
            return {"status": "ok", "zone": zone, "hwnd": hwnd}

        raise ValueError(f"Unknown action: {action!r}")

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _resolve_coords(cmd: Command, *, strict_target: bool = False) -> tuple[int, int, str]:
        """Return (x, y) via: explicit params → UIAutomation → explicit coords → cursor.

        Fallback chain:
          1. Explicit x/y in params  (vision grounder, touch)
             — if params["snap_nearest"] is set (tilt-tap), magnetically snap to
               the nearest clickable target within the snap radius first.
          2. UIAutomation structured element lookup  ← Sprint 6
          3. Explicit click coords from Command (gaze_coords)
          4. Current cursor position

        When ``strict_target`` is set and the caller named a target that none of
        tiers 1–3 could resolve, raise ``TargetResolutionError`` instead of
        silently clicking the cursor position (EH-1 / E2). Callers without a
        named target (tilt/voice-click) keep the cursor fallback.
        """
        p = cmd.params
        if "x" in p and "y" in p:
            x, y = int(p["x"]), int(p["y"])
            if p.get("snap_nearest"):
                snapped = _magnetic_snap(x, y)
                if snapped is not None:
                    return snapped[0], snapped[1], "explicit"
            return x, y, "explicit"

        # UIAutomation: try structured element lookup when target text is available.
        # The COM walk runs on the dedicated apartment thread (see
        # _run_on_com_thread) so the provider's cached COM pointer is never
        # touched from a rotating asyncio.to_thread worker.
        target = (p.get("target") or cmd.text or "").strip()
        if target and len(target.split()) <= 5:  # skip long sentences
            coords = _run_on_com_thread(_uia_find_target, target)
            if coords is not None:
                return coords[0], coords[1], "uia"

        if cmd.gaze_coords:
            return cmd.gaze_coords[0], cmd.gaze_coords[1], "gaze"

        # A named target that nothing could resolve — surface it rather than
        # clicking blindly at the cursor (which would be recorded as a success).
        if strict_target and target:
            raise TargetResolutionError(target)

        # Fall back to current cursor position
        import pyautogui
        cursor = pyautogui.position()
        return cursor[0], cursor[1], "cursor"
