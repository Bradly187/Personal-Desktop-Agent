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
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Import tool functions directly from the MCP server package
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent / "mcp_server"))
from tools import keyboard, mouse, screen, windows

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Amazon Polly TTS — spoken clarification for cloud-routed commands
# ---------------------------------------------------------------------------

_POLLY_VOICE = "Gregory"          # en-US Neural male; natural for a desktop assistant
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
        while sd.get_stream().active:
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
    source: str                          # touch | trackpad | sound_action | gaze_dwell | ...
    whisper_logprob: float = 0.0
    gesture_confidence: float = 1.0
    session_context: list[str] = field(default_factory=list)
    gaze_coords: tuple[int, int] | None = None
    params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class CommandExecutor:
    """Translates a Command into one or more desktop tool calls."""

    async def execute(self, cmd: Command) -> dict:
        """Execute a command and return a result dict."""
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

        try:
            result = await asyncio.to_thread(self._dispatch, action, cmd)
            log.info("Executed %s [source=%s]: %s", action, cmd.source, result)
            return {"status": "ok", "action": action, "result": result}
        except Exception as exc:
            log.error("Failed to execute %s: %s", action, exc)
            return {"status": "error", "action": action, "error": str(exc)}

    def _dispatch(self, action: str, cmd: Command) -> dict:
        p = cmd.params

        # ------------------------------------------------------------------ #
        # CLICK — left-click at provided coords, gaze coords, or screen centre
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
        # OPEN — Win+S search for the named app / file
        # ------------------------------------------------------------------ #
        if action == "OPEN":
            target = p.get("target", cmd.text)
            keyboard.keyboard_hotkey("win", "s")
            time.sleep(0.4)
            keyboard.keyboard_type(target)
            time.sleep(0.3)
            keyboard.keyboard_press("enter")
            return {"opened": target}

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
                from polly_stream import get_client as _get_tts
                spoken = _get_tts().speak_sync(message)
            except Exception as _tts_exc:
                log.debug("TTS speak failed, falling back to legacy Polly: %s", _tts_exc)
                if p.get("route") == "cloud":
                    spoken = _polly_speak(message)
            return {"clarify": True, "message": message, "spoken": spoken}

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
            return {"opened": url}

        # ------------------------------------------------------------------ #
        # READ_SCREEN — capture screenshot for vision model consumption
        # ------------------------------------------------------------------ #
        if action == "READ_SCREEN":
            region = p.get("region")
            return screen.screenshot(region=region)

        raise ValueError(f"Unknown action: {action!r}")

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _resolve_coords(cmd: Command) -> tuple[int, int]:
        """Return (x, y) from explicit params → gaze coords → current cursor position."""
        p = cmd.params
        if "x" in p and "y" in p:
            return int(p["x"]), int(p["y"])
        if cmd.gaze_coords:
            return cmd.gaze_coords
        # Fall back to current cursor position (click where the user is looking/pointing)
        import pyautogui
        return pyautogui.position()
