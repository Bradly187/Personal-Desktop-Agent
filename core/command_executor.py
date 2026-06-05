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
import io
import logging
import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# If set, all desktop actions are forwarded to the Windows action proxy via HTTP.
# Set by start_agent_wsl.sh: WSL_ACTION_PROXY=http://127.0.0.1:8768
_PROXY_URL: str | None = os.environ.get("WSL_ACTION_PROXY")

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
import os as _os
try:
    _SNAP_RADIUS_PX = int(_os.environ.get("DA_SNAP_RADIUS_PX", "200"))
except ValueError:
    _SNAP_RADIUS_PX = 200


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

    # Fallback — direct UIA walk.
    provider = _get_ui_provider()
    if not (provider and provider.is_available()):
        return None
    try:
        elem = provider.find_nearest_clickable(x, y, max_radius=_SNAP_RADIUS_PX)
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
# ---------------------------------------------------------------------------
_KNOWN_APPS: dict[str, str] = {
    # Kiro IDE (phonetic aliases for Whisper misrecognitions)
    "kiro":             r"C:\Users\bradt\AppData\Local\Programs\Kiro\Kiro.exe",
    "kiro ide":         r"C:\Users\bradt\AppData\Local\Programs\Kiro\Kiro.exe",
    "key row":          r"C:\Users\bradt\AppData\Local\Programs\Kiro\Kiro.exe",
    "key-row":          r"C:\Users\bradt\AppData\Local\Programs\Kiro\Kiro.exe",
    "cairo":            r"C:\Users\bradt\AppData\Local\Programs\Kiro\Kiro.exe",
    # VS Code
    "vs code":          r"C:\Users\bradt\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "vscode":           r"C:\Users\bradt\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "visual studio code": r"C:\Users\bradt\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "code":             r"C:\Users\bradt\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    # Terminal
    "terminal":         r"C:\Users\bradt\AppData\Local\Microsoft\WindowsApps\wt.exe",
    "windows terminal": r"C:\Users\bradt\AppData\Local\Microsoft\WindowsApps\wt.exe",
    # Chrome
    "chrome":           r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome":    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # Slack
    "slack":            r"C:\Users\bradt\AppData\Local\slack\slack.exe",
    # Discord
    "discord":          r"C:\Users\bradt\AppData\Local\Discord\app-1.0.9237\Discord.exe",
}

# URL shortcuts — opened with the default browser via os.startfile.
_KNOWN_URLS: dict[str, str] = {
    "claude":           "https://claude.ai",
    "claude ai":        "https://claude.ai",
    "linkedin":         "https://linkedin.com",
    "github":           "https://github.com",
}


# ---------------------------------------------------------------------------
# Amazon Polly TTS — spoken clarification for cloud-routed commands
# ---------------------------------------------------------------------------

_POLLY_VOICE = "Danielle"         # en-US Generative female; also supports Long-form
_POLLY_SAMPLE_RATE = 16_000       # 16 kHz PCM matches sounddevice default input rate
_POLLY_MAX_CHARS = 3_000          # Polly hard limit for standard text input
_POLLY_TIMEOUT_S = 5              # boto3 connect + read timeout


def _polly_speak(message: str) -> bool:
    """Speak a clarification message via Amazon Polly Neural TTS.

    Called from _dispatch() which already runs in asyncio.to_thread, so
    this function is safe to block.

    Returns True if audio played successfully; False on any error
    (missing credentials, network timeout, sounddevice failure, etc.).
    All exceptions are caught internally — never raises.
    """
    if not message:
        return False

    if len(message) > _POLLY_MAX_CHARS:
        message = message[:_POLLY_MAX_CHARS]

    try:
        import boto3
        from botocore.config import Config
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        log.debug("Polly TTS: dependency missing (%s) — install boto3, numpy, sounddevice", exc)
        return False

    try:
        cfg = Config(connect_timeout=_POLLY_TIMEOUT_S, read_timeout=_POLLY_TIMEOUT_S)
        polly = boto3.client("polly", region_name="us-east-1", config=cfg)

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
        timeout_s = duration_s + _POLLY_TIMEOUT_S  # audio duration + network buffer

        sd.play(audio, samplerate=_POLLY_SAMPLE_RATE)
        deadline = time.monotonic() + timeout_s
        while sd.get_stream() and sd.get_stream().active:
            if time.monotonic() > deadline:
                sd.stop()
                log.warning("Polly TTS: playback timed out after %.1fs", timeout_s)
                return False
            time.sleep(0.05)

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


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class CommandExecutor:
    """Translates a Command into one or more desktop tool calls."""

    async def _proxy_execute(self, cmd: Command) -> dict:
        """Forward the command to the Windows action proxy (WSL mode).

        The proxy (windows_action_proxy.py) runs under Windows Python with
        full pyautogui/win32gui access.  Called when WSL_ACTION_PROXY is set.
        """
        import aiohttp
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
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_PROXY_URL}/execute",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30.0),
                ) as resp:
                    return await resp.json()
        except Exception as exc:
            log.error("Windows action proxy unreachable (%s): %s — is start_windows_proxy.bat running?",
                      _PROXY_URL, exc)
            return {"status": "error", "action": cmd.action,
                    "error": f"proxy unreachable: {exc}"}

    async def execute(self, cmd: Command) -> dict:
        """Execute a command and return a result dict."""
        # WSL mode: delegate all desktop actions to the Windows proxy process.
        if _PROXY_URL:
            return await self._proxy_execute(cmd)

        action = cmd.action.upper()

        # MOUSEDOWN/MOUSEUP must be synchronous — they're timing-critical
        # for drag-select and must not compete with trackpad moves for threading
        if action in ("MOUSEDOWN", "MOUSEUP"):
            try:
                import pyautogui
                x, y = pyautogui.position()
                if action == "MOUSEDOWN":
                    pyautogui.mouseDown(x, y, button='left', _pause=False)
                    result = {"mousedown": True, "x": x, "y": y}
                else:
                    pyautogui.mouseUp(x, y, button='left', _pause=False)
                    result = {"mouseup": True, "x": x, "y": y}
                log.info("Executed %s [source=%s]: %s", action, cmd.source, result)
                return {"status": "ok", "action": action, "result": result}
            except Exception as exc:
                log.error("Failed to execute %s: %s", action, exc)
                return {"status": "error", "action": action, "error": str(exc)}

        # Take a pre-action snapshot for verifiable verbs
        from desktop.action_verifier import VERIFIABLE_VERBS
        verifier = _get_action_verifier()
        pre_b64 = ""
        if verifier and action in VERIFIABLE_VERBS:
            pre_b64 = await asyncio.to_thread(verifier.snapshot)

        try:
            result = await asyncio.to_thread(self._dispatch, action, cmd)
            log.info("Executed %s [source=%s]: %s", action, cmd.source, result)
        except Exception as exc:
            log.error("Failed to execute %s: %s", action, exc)
            return {"status": "error", "action": action, "error": str(exc)}

        # Post-action verification — perceptual diff confirms screen changed
        verify_result = None
        if verifier and pre_b64:
            vr = await asyncio.to_thread(verifier.verify, pre_b64, action)
            verify_result = {"success": vr.success, "diff_pct": vr.diff_pct,
                             "reason": vr.reason, "elapsed_ms": vr.elapsed_ms}
            if not vr.success:
                log.warning(
                    "ActionVerifier: %s — no visible change (diff=%.1f%%, %dms)",
                    action, vr.diff_pct, vr.elapsed_ms,
                )

        resp = {"status": "ok", "action": action, "result": result}
        if verify_result:
            resp["verification"] = verify_result
        return resp

    def _dispatch(self, action: str, cmd: Command) -> dict:
        p = cmd.params

        # ------------------------------------------------------------------ #
        # CLICK — left-click at provided coords, explicit click coords, or screen centre
        # ------------------------------------------------------------------ #
        if action == "CLICK":
            x, y = self._resolve_coords(cmd)
            btn = p.get("button", "left")
            return mouse.mouse_click(x, y, button=btn)

        # ------------------------------------------------------------------ #
        # SCROLL
        # ------------------------------------------------------------------ #
        if action == "SCROLL":
            x, y = self._resolve_coords(cmd)
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
                import subprocess
                subprocess.Popen([exe])
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
            path = Path(p.get("path", "").strip())
            content = p.get("content", cmd.text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"written": str(path), "bytes": len(content)}

        # ------------------------------------------------------------------ #
        # RUN_TERMINAL — run a shell command and capture output
        # params: {command: str, cwd: str | None}
        # ------------------------------------------------------------------ #
        if action == "RUN_TERMINAL":
            command = p.get("command", cmd.text)
            cwd = p.get("cwd") or None
            result = subprocess.run(
                command, shell=True, capture_output=True,
                text=True, timeout=60, cwd=cwd,
            )
            return {
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
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
    def _resolve_coords(cmd: Command) -> tuple[int, int]:
        """Return (x, y) via: explicit params → UIAutomation → explicit coords → cursor.

        Fallback chain:
          1. Explicit x/y in params  (vision grounder, touch)
             — if params["snap_nearest"] is set (tilt-tap), magnetically snap to
               the nearest clickable target within the snap radius first.
          2. UIAutomation structured element lookup  ← Sprint 6
          3. Explicit click coords from Command (gaze_coords)
          4. Current cursor position
        """
        p = cmd.params
        if "x" in p and "y" in p:
            x, y = int(p["x"]), int(p["y"])
            if p.get("snap_nearest"):
                snapped = _magnetic_snap(x, y)
                if snapped is not None:
                    return snapped
            return x, y

        # UIAutomation: try structured element lookup when target text is available
        target = (p.get("target") or cmd.text or "").strip()
        if target and len(target.split()) <= 5:  # skip long sentences
            provider = _get_ui_provider()
            if provider and provider.is_available():
                try:
                    element = provider.find(target, timeout_s=0.3)
                    if element and element.is_enabled:
                        cx, cy = element.center()
                        log.info(
                            "UIAutomation resolved %r → (%d, %d) [%s]",
                            target, cx, cy, element.role,
                        )
                        return cx, cy
                except Exception as exc:
                    log.debug("UIAutomation lookup failed: %s", exc)

        if cmd.gaze_coords:
            return cmd.gaze_coords
        # Fall back to current cursor position
        import pyautogui
        return pyautogui.position()
