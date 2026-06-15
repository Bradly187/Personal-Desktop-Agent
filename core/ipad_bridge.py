"""iPad Bridge — WebSocket server for the Personal Desktop Agent

Listens on port 8765 for messages from the iPad app and dispatches them
to FusionEngine, CommandExecutor, or directly to pyautogui.

Message types:
  touch_command          →  CommandExecutor (action routing; priority 1)
  trackpad               →  direct pyautogui (mouse/scroll; no LLM)
  handwriting_image      →  pix2tex OCR → handwriting_result reply
  ping                   →  immediate pong echo (latency measurement)
  tilt_position          →  FusionEngine.on_tilt_position() (absolute positioning)
  tilt                   →  FusionEngine.on_tilt() (legacy velocity mode)
  tilt_tap               →  FusionEngine.on_touch() (when wired)
  tilt_ratchet           →  FusionEngine.on_tilt_ratchet() (re-center neutral point)
  sensor_switch          →  FusionEngine.on_sensor_switch() (mutual-exclusion toggle)
  cursor_pause           →  FusionEngine.on_cursor_pause() (quick-pause all cursor sensors)
  cursor_resume          →  FusionEngine.on_cursor_resume() (resume cursor sensors)
  keyword                →  FusionEngine.on_keyword() (when wired)
  depth_frame            →  LiDARReceiver.on_depth_frame() (when wired)
  camera_frame           →  GestureProcessor.on_camera_frame() (when wired)
  audio_stream           →  WhisperStream.on_audio_chunk() (VAD + Whisper transcription)
  set_dwell_action       →  updates active dwell action type (left_click, right_click, etc.)
  set_feature_toggle     →  updates a FusionEngine feature toggle (currently none defined)
  gesture_assessment     →  sets disabled gesture list in GestureProcessor
  pain_day_override      →  BehavioralTwinState.set_manual_pain_day(); relaxes VAD threshold
  calibration_start      →  spawns VoiceCalibrator session task; streams progress to iPad
  calibration_cancel     →  VoiceCalibrator.stop()
  mic_mute               →  WhisperStream.set_muted(bool) — hard mute/unmute mic input
                            (PC echoes a `mic_state` push so the iPad indicator syncs)
  ipad_log               →  structured log entries forwarded to AgentDB ipad_logs table

Usage:
  python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

mDNS advertisement: _desktop-agent._tcp.local. on the chosen port
so the iPad app can discover the PC without manual IP entry.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

# Make mcp_server/tools importable without install
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent / "mcp_server"))

from core.command_executor import Command, CommandExecutor

log = logging.getLogger("ipad_bridge")

# TYPE_CHECKING imports — avoid circular deps at runtime
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from core.fusion_engine import FusionEngine
    from sensors.gesture_processor import GestureProcessor
    from sensors.lidar_receiver import LiDARReceiver
    from sensors.sensor_viewer import SensorViewer
    from sensors.whisper_stream import WhisperStream

# ---------------------------------------------------------------------------
# Optional mDNS — graceful if zeroconf unavailable
# ---------------------------------------------------------------------------

try:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False
    log.warning("zeroconf not installed — mDNS discovery disabled. "
                "Install with: pip install zeroconf")


# ---------------------------------------------------------------------------
# Pairing token (C1) — authenticate every WebSocket connection
# ---------------------------------------------------------------------------

_BRIDGE_TOKEN_DIR = Path.home() / ".claude" / "ipad_bridge"
_BRIDGE_TOKEN_PATH = _BRIDGE_TOKEN_DIR / "paired_token"

# Binary audio transport: when enabled the bridge advertises `binary_audio` in
# its welcome frame and accepts raw binary WebSocket frames for audio (no base64,
# no JSON envelope). DA_BINARY_AUDIO=0 disables the advertisement → connected
# iPads fall back to the legacy base64 `audio_stream` text path.
_BINARY_AUDIO_ENABLED = os.environ.get("DA_BINARY_AUDIO", "1") != "0"

# Binary frame message tags (byte 0). Only AUDIO_PCM16 is defined today; the
# tag byte keeps the binary channel extensible without a JSON `type` field.
_BIN_TAG_AUDIO_PCM16 = 0x01


def _load_or_create_bridge_token() -> str:
    """Return the iPad pairing token, generating one on first run.

    Stored at ``~/.claude/ipad_bridge/paired_token`` (0600). Every WS client
    must present this token (``X-Agent-Token`` header or ``?token=`` query) or
    the connection is rejected before it is accepted. Fail-closed: if the token
    cannot be read or created we raise rather than run an open bridge.
    """
    try:
        if _BRIDGE_TOKEN_PATH.exists():
            tok = _BRIDGE_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        _BRIDGE_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(32)
        _BRIDGE_TOKEN_PATH.write_text(tok, encoding="utf-8")
        try:
            os.chmod(_BRIDGE_TOKEN_PATH, 0o600)
        except OSError:
            pass  # best-effort; Windows ACLs differ
        return tok
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read or create the iPad bridge pairing token at "
            f"{_BRIDGE_TOKEN_PATH}: {exc}. Refusing to start an unauthenticated bridge."
        ) from exc


# ---------------------------------------------------------------------------
# iPad Bridge
# ---------------------------------------------------------------------------

class IPadBridge:
    # Valid dwell action types for set_dwell_action messages
    VALID_DWELL_ACTIONS: set[str] = {
        "left_click", "right_click", "double_click", "drag_start", "drag_end"
    }

    # Valid feature names for set_feature_toggle messages. All previous features
    # were gaze-pipeline toggles and were removed; the set is empty (no features
    # are currently toggleable) while the message path stays wired.
    VALID_FEATURES: set[str] = set()

    # Window in seconds within which an iPad log entry is correlated to the
    # most recent PC-side trace_id. iPad log batches are sent asynchronously
    # so a 2 s window covers most sensor round-trips without false attribution.
    _TRACE_WINDOW_S: float = 2.0

    def __init__(self, port: int = 8765, host: str = "0.0.0.0",
                 token: Optional[str] = None):
        self.port = port
        self.host = host

        # Pairing token (C1): required on every WS connection. Injectable for
        # tests; otherwise loaded/generated under ~/.claude/ipad_bridge/.
        self._token = token or _load_or_create_bridge_token()

        # Screen dimensions (resolved lazily at start)
        self._screen_w: int = 1920
        self._screen_h: int = 1080

        # Active dwell action type (default: left_click)
        self._active_dwell_action: str = "left_click"

        # Direct executor for Phase 1 touch/trackpad commands
        self._executor = CommandExecutor()

        # Phase 2+ pipeline components (wired by main.py)
        self._fusion: Optional["FusionEngine"] = None
        self._lidar: Optional["LiDARReceiver"] = None
        self._gesture: Optional["GestureProcessor"] = None
        self._whisper: Optional["WhisperStream"] = None
        self._viewer: Optional["SensorViewer"] = None

        # DB for persistent iPad log storage (wired by main.py)
        self._agent_db = None
        self._session_id: int = -1

        # Tracked DB writer tasks: keeps fire-and-forget create_task() refs alive
        # until completion (otherwise Python may GC them) and surfaces exceptions
        # via _on_ipad_log_task_done. Mirrors the FusionEngine._route_tasks pattern.
        self._ipad_log_tasks: set[asyncio.Task] = set()

        # iPad↔PC trace correlation: the most recent PC-side trace_id and the wall
        # time it was set. ipad_log entries arriving within _TRACE_WINDOW_S of a
        # command execution are tagged with this trace_id so they can be joined to
        # commands.trace_id in AgentDB.
        self._active_trace_id: Optional[str] = None
        self._active_trace_ts: float = 0.0

        self._clients: set[web.WebSocketResponse] = set()
        self._zeroconf: Any = None

        # A2UI: surfaces awaiting a tap reply, keyed by surface_id → Future.
        # Phase 1 (approval) writes the response file directly; Phase 2+ awaits
        # these futures for enumerable CLARIFY answers.
        self._a2ui_pending: dict[str, "asyncio.Future"] = {}
        self._approval_dir = Path.home() / ".claude" / "approval"

    # ---------------------------------------------------------------------- #
    # Wiring (called by main.py before run())
    # ---------------------------------------------------------------------- #

    def set_fusion_engine(self, fusion: "FusionEngine") -> None:
        self._fusion = fusion

    def set_lidar(self, lidar: "LiDARReceiver") -> None:
        self._lidar = lidar

    def set_gesture_processor(self, gesture: "GestureProcessor") -> None:
        self._gesture = gesture

    def set_whisper_stream(self, whisper: "WhisperStream") -> None:
        self._whisper = whisper
        # Push every mute-state change (voice- or iPad-initiated) to all clients
        # so the iPad's mute indicator stays in sync.
        whisper.on_mute_change = self._notify_mic_state

    def set_coordinator(self, coordinator) -> None:
        self._coordinator = coordinator

    def set_viewer(self, viewer: "SensorViewer") -> None:
        self._viewer = viewer

    def set_agent_db(self, agent_db, session_id: int) -> None:
        self._agent_db = agent_db
        self._session_id = session_id

    def set_active_trace_id(self, trace_id: Optional[str]) -> None:
        """Record the most recent PC-side trace_id for iPad log correlation.

        Called by HybridCoordinator after each command execution so ipad_log
        entries that arrive within _TRACE_WINDOW_S can be joined to the command
        span via commands.trace_id in AgentDB.
        """
        self._active_trace_id = trace_id
        self._active_trace_ts = time.time()

    # ---------------------------------------------------------------------- #
    # Startup
    # ---------------------------------------------------------------------- #

    async def _resolve_screen_size(self) -> None:
        """Resolve screen dimensions at startup."""
        try:
            import pyautogui
            self._screen_w, self._screen_h = pyautogui.size()
        except Exception:
            pass  # keep defaults

    # ---------------------------------------------------------------------- #
    # WebSocket handler
    # ---------------------------------------------------------------------- #

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        # ── Pairing-token gate (C1): reject before the socket is accepted ──
        supplied = request.headers.get("X-Agent-Token") or request.query.get("token", "")
        if not supplied or not hmac.compare_digest(
                supplied.encode("utf-8"), self._token.encode("utf-8")):
            log.warning("Rejected unauthenticated WS connection from %s", request.remote)
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        peer = request.remote
        log.info("Client connected: %s", peer)
        self._clients.add(ws)

        # Send welcome message so the iPad client confirms the connection is alive
        # (WebSocketManager waits for first receive before transitioning to .connected)
        try:
            await ws.send_json({
                "type": "status",
                "active_window": None,
                "cursor": {"x": 0, "y": 0},
                "active_dwell_action": self._active_dwell_action,
                "muted": bool(self._whisper and self._whisper._muted),
                # Capability advertisement: when present+true the iPad may stream
                # audio as raw binary frames instead of base64-in-JSON. Old PCs
                # omit it → iPad keeps the base64 path (backward compatible).
                "binary_audio": _BINARY_AUDIO_ENABLED,
                "ts": time.time(),
            })
        except Exception as exc:
            log.debug("Failed to send welcome: %s", exc)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._handle_binary(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.error("WS error from %s: %s", peer, ws.exception())
        finally:
            self._clients.discard(ws)
            # Safety: release mouse button if client disconnects during select mode
            if self._mouse_down:
                log.warning("Client disconnected with mouse held — releasing")
                import pyautogui
                pyautogui.mouseUp(_pause=False)
                self._mouse_down = False
            log.info("Client disconnected: %s", peer)

        return ws

    # ---------------------------------------------------------------------- #
    # Binary frame routing (audio)
    # ---------------------------------------------------------------------- #

    def _handle_binary(self, data: bytes) -> None:
        """Route a raw binary WebSocket frame by its 1-byte tag prefix.

        Layout: ``byte[0]`` = message tag, ``byte[1:]`` = payload. For
        ``_BIN_TAG_AUDIO_PCM16`` the payload is little-endian int16 PCM streamed
        straight into WhisperStream (no base64/JSON). Mirrors the per-handler
        try/except discipline of the text path — a malformed frame is logged and
        dropped, never raised into the receive loop.
        """
        try:
            if not data:
                return
            tag = data[0]
            if tag == _BIN_TAG_AUDIO_PCM16:
                if self._whisper and self._whisper.available:
                    payload = data[1:]
                    self._whisper.on_audio_chunk_pcm(payload, len(payload) // 2)
                else:
                    log.debug("binary audio received (WhisperStream not wired/available)")
            else:
                log.debug("Unknown binary tag 0x%02x (%d bytes) — dropped", tag, len(data))
        except Exception as exc:
            log.warning("ipad_bridge._handle_binary error: %s", exc)

    # ---------------------------------------------------------------------- #
    # Message routing
    # ---------------------------------------------------------------------- #

    async def _handle_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("Bad JSON: %s", exc)
            await self._ack(ws, None, "error", "invalid JSON")
            return

        msg_type = msg.get("type", "unknown")
        msg_id = msg.get("id")

        log.debug("Received [%s] id=%s", msg_type, msg_id)

        # ------------------------------------------------------------------ #
        # Ping/pong — immediate echo for latency measurement
        # ------------------------------------------------------------------ #
        if msg_type == "ping":
            await ws.send_json({"type": "pong", "id": msg_id, "t": msg.get("t", 0)})
            return

        # ------------------------------------------------------------------ #
        # Phase 1 message handlers
        # ------------------------------------------------------------------ #
        if msg_type == "touch_command":
            await self._handle_touch_command(ws, msg)
            return

        # ------------------------------------------------------------------ #
        # Direct trackpad — bypasses FusionEngine, no LLM
        # ------------------------------------------------------------------ #
        if msg_type == "trackpad":
            result = await self._handle_trackpad(msg)
            if result.get("error"):
                await self._ack(ws, msg_id, "error", result["error"])
            return

        # ------------------------------------------------------------------ #
        # Handwriting OCR — async with immediate ack
        # ------------------------------------------------------------------ #
        if msg_type == "handwriting_image":
            await self._handle_handwriting(ws, msg)
            return

        # ------------------------------------------------------------------ #
        # Dwell action selection — updates active dwell action type
        # ------------------------------------------------------------------ #
        if msg_type == "set_dwell_action":
            await self._handle_set_dwell_action(ws, msg)
            return

        if msg_type == "set_feature_toggle":
            await self._handle_set_feature_toggle(ws, msg)
            return

        # ------------------------------------------------------------------ #
        # Sensor streams — dispatched to FusionEngine / Phase 3 components
        # No ack sent for high-frequency streams (tilt).
        # All sensor handlers are wrapped in try/except to prevent malformed
        # data from crashing the bridge.
        # ------------------------------------------------------------------ #
        if msg_type == "tilt_position":
            try:
                if self._fusion:
                    x = float(msg.get("x", 0.5))
                    y = float(msg.get("y", 0.5))
                    self._fusion.on_tilt_position(x, y)
            except (ValueError, TypeError) as exc:
                log.debug("Bad tilt_position data: %s", exc)
            return

        if msg_type == "tilt":
            try:
                if self._fusion:
                    rx = float(msg.get("rx", 0.0))
                    ry = float(msg.get("ry", 0.0))
                    self._fusion.on_tilt(rx, ry)
            except (ValueError, TypeError) as exc:
                log.debug("Bad tilt data: %s", exc)
            return

        if msg_type == "tilt_tap":
            log.info("tilt_tap received (id=%s) → CLICK", msg.get("id", "?"))
            if self._fusion:
                cmd = Command(text="tilt_tap", action="CLICK", source="touch")
                self._fusion.on_touch(cmd)
            else:
                log.warning("tilt_tap dropped: FusionEngine not wired")
            return

        if msg_type == "dwell_click":
            # Tilt dwell-to-click: the iPad held the cursor still long enough.
            # Fire whatever the DwellActionToolbar has selected, at the current
            # cursor position (tilt already positioned it).
            result = await self._handle_dwell_click()
            await self._ack(ws, msg.get("id"), result.get("status", "ok"),
                            result.get("action", self._active_dwell_action))
            return

        if msg_type == "tilt_ratchet":
            if self._fusion:
                self._fusion.on_tilt_ratchet()
            return

        if msg_type == "sensor_switch":
            if self._fusion:
                from_sensor = msg.get("from")
                to_sensor = msg.get("to", "tilt")
                self._fusion.on_sensor_switch(from_sensor, to_sensor)
            return

        if msg_type == "cursor_pause":
            if self._fusion:
                self._fusion.on_cursor_pause()
            return

        if msg_type == "cursor_resume":
            if self._fusion:
                self._fusion.on_cursor_resume()
            return


        if msg_type == "keyword":
            if self._fusion:
                word = str(msg.get("word", ""))
                conf = float(msg.get("confidence", 1.0))
                self._fusion.on_keyword(word, conf)
            return

        if msg_type == "depth_frame":
            if self._lidar:
                self._lidar.on_depth_frame(msg)
            else:
                log.debug("depth_frame received but LiDARReceiver not wired")
            if self._viewer:
                self._viewer.on_depth_frame(msg)
            return

        if msg_type == "camera_frame":
            if self._gesture:
                gesture_cmd = self._gesture.on_camera_frame(msg)
                if gesture_cmd and self._fusion:
                    self._fusion.on_gesture(gesture_cmd)
                # Forward landmarks to viewer overlay
                if self._viewer:
                    self._viewer.on_hand_landmarks(self._gesture.latest_landmarks)
            else:
                log.debug("camera_frame received but GestureProcessor not wired")
            if self._viewer:
                self._viewer.on_camera_frame(msg)
            return

        if msg_type == "gesture_assessment":
            # {"type": "gesture_assessment", "disabled": ["PINCH", "FIST"]}
            disabled = set(msg.get("disabled", []))
            if self._gesture:
                self._gesture.set_disabled_gestures(disabled)
            await self._ack(ws, msg.get("id"), "ok", "gesture_assessment applied")
            return

        if msg_type == "pain_day_override":
            active = bool(msg.get("active", False))
            log.info("ipad_bridge: pain_day_override active=%s", active)
            if self._coordinator and hasattr(self._coordinator, "_twin") \
                    and self._coordinator._twin:
                self._coordinator._twin.set_manual_pain_day(active)
            # Immediate recognizer relaxation (VAD + logprob floor) so the next
            # utterance benefits before route() reconciles on the next command.
            if self._whisper is not None:
                self._whisper.apply_pain_day(active)
            await self._ack(ws, msg.get("id"), "ok", "pain_day_override applied")
            return

        if msg_type == "flare_profile":
            # Which sensors degrade on a flare (iPad FlareProfileSheet). Persist
            # and apply live so route() honours them on the next command.
            flags = {
                k: msg[k] for k in (
                    "voice_degrades", "gesture_degrades", "tilt_degrades",
                    "sound_degrades", "flare_vad_scale",
                ) if k in msg
            }
            log.info("ipad_bridge: flare_profile %s", flags)
            if self._agent_db:
                await self._agent_db.upsert_flare_profile(flags)
            if self._coordinator and hasattr(self._coordinator, "_twin") \
                    and self._coordinator._twin:
                self._coordinator._twin.set_flare_profile(flags)
            await self._ack(ws, msg.get("id"), "ok", "flare_profile applied")
            return

        if msg_type == "calibration_start":
            condition = msg.get("condition", "good_day")
            quick = bool(msg.get("quick", False))
            log.info("ipad_bridge: calibration_start condition=%s quick=%s", condition, quick)
            if self._coordinator and self._coordinator._calibrator:
                asyncio.create_task(
                    self._run_calibration_with_progress(ws, condition, quick)
                )
            await self._ack(ws, msg.get("id"), "ok", "calibration started")
            return

        if msg_type == "calibration_cancel":
            if self._coordinator and self._coordinator._calibrator:
                self._coordinator._calibrator.stop()
            await self._ack(ws, msg.get("id"), "ok", "calibration cancelled")
            return


        if msg_type == "mic_mute":
            muted = bool(msg.get("muted", True))
            if self._whisper is not None:
                self._whisper.set_muted(muted)
            log.info("ipad_bridge: mic %s via iPad button", "MUTED" if muted else "UNMUTED")
            await self._ack(ws, msg.get("id"), "ok",
                            "mic muted" if muted else "mic unmuted")
            return

        if msg_type == "audio_stream":
            if self._whisper and self._whisper.available:
                samples_b64 = msg.get("samples", "")
                frames = int(msg.get("frames", 0))
                if samples_b64:
                    self._whisper.on_audio_chunk(samples_b64, frames)
            else:
                log.debug("audio_stream received (WhisperStream not wired or unavailable)")
            return

        if msg_type == "ipad_log":
            await self._handle_ipad_log(msg)
            return  # fire-and-forget, no ack

        if msg_type == "a2ui_event":
            await self._handle_a2ui_event(ws, msg)
            return

        log.warning("Unknown message type: %s", msg_type)
        await self._ack(ws, msg_id, "error", f"unknown type: {msg_type}")

    # ---------------------------------------------------------------------- #
    # Voice calibration — PC drives the session, iPad shows progress
    # ---------------------------------------------------------------------- #

    async def _run_calibration_with_progress(
        self,
        ws: web.WebSocketResponse,
        condition: str,
        quick: bool,
    ) -> None:
        """Run VoiceCalibrator and stream phrase/result events back to iPad."""
        calibrator = self._coordinator._calibrator

        def _on_progress(index: int, total: int, result) -> None:
            # Called from the event loop (via asyncio.create_task inside run())
            asyncio.create_task(ws.send_json({
                "type": "calibration_result",
                "expected": result.expected,
                "heard": result.heard,
                "matched": result.match,
            }))

        # Monkey-patch VoiceCalibrator to also send phrase prompts to the iPad
        original_speak = calibrator._speak_safe

        def _speak_with_ipad(text: str) -> None:
            original_speak(text)
            # Extract phrase from "Say: <phrase>" prompts
            if text.lower().startswith("say:"):
                phrase = text[4:].strip()
                # We'll send phrase prompts in the next iteration's ack
                # Store for the progress callback to pick up
                self._pending_calibration_phrase = phrase

        calibrator._speak_safe = _speak_with_ipad

        try:
            report = await calibrator.run(
                condition=condition,
                quick=quick,
                on_progress=lambda idx, total, result: (
                    _on_progress(idx, total, result),
                    asyncio.create_task(ws.send_json({
                        "type": "calibration_phrase",
                        "phrase": getattr(self, "_pending_calibration_phrase", ""),
                        "index": idx,
                        "total": total,
                    })) if idx < total else None,
                ),
            )
            await ws.send_json({
                "type": "calibration_complete",
                "accuracy": round(report.accuracy, 3),
                "corrections": report.corrections_added,
                "condition": report.condition,
            })
        except Exception as exc:
            log.error("ipad_bridge: calibration error: %s", exc)
            await ws.send_json({
                "type": "calibration_error",
                "message": str(exc),
            })
        finally:
            calibrator._speak_safe = original_speak

    # ---------------------------------------------------------------------- #
    # touch_command — routed through FusionEngine (priority 1)
    # ---------------------------------------------------------------------- #

    async def _handle_touch_command(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Execute a touch command directly via CommandExecutor (Phase 1)."""
        msg_id = msg.get("id")
        action = msg.get("action", "").upper()
        if not action:
            await self._ack(ws, msg_id, "error", "missing 'action' field")
            return

        # Track mouse button state for select mode
        if action == "MOUSEDOWN":
            self._mouse_down = True
        elif action == "MOUSEUP":
            self._mouse_down = False

        cmd = Command(
            text=msg.get("text", action),
            action=action,
            source="touch",
            params=msg.get("params", {}),
        )

        # Direct execution — no FusionEngine in Phase 1
        result = await self._executor.execute(cmd)
        await self._ack(ws, msg_id, result.get("status", "ok"), result.get("error"))
        if "image_base64" in result:
            await self._send_screenshot(ws, msg_id, result["image_base64"])
        # Skip _send_status for timing-sensitive actions (MOUSEDOWN/MOUSEUP/HOTKEY)
        # because get_active_window via asyncio.to_thread can race with clipboard ops
        if action not in ("MOUSEDOWN", "MOUSEUP", "HOTKEY"):
            await self._send_status(ws)

    # ---------------------------------------------------------------------- #
    # trackpad — direct pyautogui, no FusionEngine, no LLM
    # ---------------------------------------------------------------------- #

    _mouse_down: bool = False  # Track if mouse button is held (select mode)

    async def _handle_trackpad(self, msg: dict) -> dict:
        """Handle trackpad events. Moves use pyautogui.moveRel() synchronously
        (single Win32 API call, <1ms). No accumulator, no threading for moves.
        This keeps the event loop responsive so MOUSEUP is never blocked."""
        import pyautogui

        event = msg.get("event", "")
        try:
            if event == "move":
                dx, dy = int(msg.get("dx", 0)), int(msg.get("dy", 0))
                if dx == 0 and dy == 0:
                    return {"status": "ok"}
                # moveRel is a direct Win32 SendInput call — takes microseconds.
                # Running synchronously means the event loop processes the next
                # message (potentially MOUSEUP) immediately after, with no backlog.
                pyautogui.moveRel(dx, dy, _pause=False)
                return {"status": "ok"}

            elif event == "tap":
                button = msg.get("button", "left")
                # Click in place — no moveTo, no coordinate lookup.
                # pyautogui.click() without x,y clicks at current cursor position.
                pyautogui.click(button=button, _pause=False)
                return {"status": "ok"}

            elif event == "scroll":
                direction = msg.get("direction", "down")
                clicks = int(msg.get("clicks", 3))
                from tools import mouse as mouse_tools
                cx, cy = pyautogui.position()
                result = await asyncio.to_thread(
                    mouse_tools.mouse_scroll, cx, cy, direction, clicks
                )
                return {"status": "ok", "result": result}

            else:
                return {"status": "error", "error": f"unknown trackpad event: {event!r}"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def _handle_dwell_click(self) -> dict:
        """Execute the active dwell action at the current cursor position.

        Triggered by the iPad's tilt dwell (hold-still) detection. Maps the
        DwellActionToolbar selection (_active_dwell_action) onto pyautogui
        calls at the current cursor — no coordinate lookup, like trackpad tap.
        """
        import pyautogui
        action = self._active_dwell_action
        try:
            # Magnetic snap (Phase 2b): for discrete clicks, jump to the nearest
            # clickable target before clicking so a dwell only needs to land in
            # the neighborhood — same area-cursor assist as the tilt-tap. Drag
            # start/end stay at the exact cursor (drags must be precise).
            if action in ("left_click", "right_click", "double_click"):
                try:
                    from core.command_executor import _magnetic_snap
                    cx, cy = pyautogui.position()
                    snapped = await asyncio.to_thread(_magnetic_snap, cx, cy)
                    if snapped is not None:
                        await asyncio.to_thread(
                            pyautogui.moveTo, snapped[0], snapped[1], duration=0
                        )
                except Exception as exc:
                    log.debug("dwell magnetic snap skipped: %s", exc)

            if action == "left_click":
                pyautogui.click(button="left", _pause=False)
            elif action == "right_click":
                pyautogui.click(button="right", _pause=False)
            elif action == "double_click":
                pyautogui.doubleClick(_pause=False)
            elif action == "drag_start":
                pyautogui.mouseDown(_pause=False)
            elif action == "drag_end":
                pyautogui.mouseUp(_pause=False)
            else:
                return {"status": "error", "action": action,
                        "error": f"unknown dwell action: {action!r}"}
            log.info("Dwell click fired: %s", action)
            return {"status": "ok", "action": action}
        except Exception as exc:
            log.warning("dwell_click (%s) failed: %s", action, exc)
            return {"status": "error", "action": action, "error": str(exc)}

    # ---------------------------------------------------------------------- #
    # Handwriting OCR
    # ---------------------------------------------------------------------- #

    async def _handle_handwriting(self, ws: web.WebSocketResponse, msg: dict) -> None:
        import base64
        from tools import handwriting as hw_tools

        msg_id = msg.get("id")
        image_b64 = msg.get("image", "")
        if not image_b64:
            await self._ack(ws, msg_id, "error", "missing 'image' field")
            return

        await self._ack(ws, msg_id, "ok")  # immediate ack — OCR takes ~1-2 s

        try:
            png_bytes = base64.b64decode(image_b64)
        except Exception as exc:
            await self._send_handwriting_result(ws, msg_id, {"error": f"bad base64: {exc}"})
            return

        result = await asyncio.to_thread(hw_tools.recognize_math, png_bytes)
        await self._send_handwriting_result(ws, msg_id, result)

    # ---------------------------------------------------------------------- #
    # set_dwell_action — updates active dwell action type
    # ---------------------------------------------------------------------- #

    async def _handle_set_dwell_action(
        self, ws: web.WebSocketResponse, msg: dict
    ) -> None:
        """Handle set_dwell_action message. Validates action_type and updates state."""
        msg_id = msg.get("id")
        action_type = msg.get("action_type")

        if action_type not in self.VALID_DWELL_ACTIONS:
            error_msg = (
                f"invalid action_type: {action_type!r}; "
                f"must be one of {sorted(self.VALID_DWELL_ACTIONS)}"
            )
            log.warning("set_dwell_action rejected: %s", error_msg)
            payload: dict = {"type": "ack", "status": "error", "error": error_msg}
            if msg_id is not None:
                payload["id"] = msg_id
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log.debug("Failed to send error ack: %s", exc)
            return

        self._active_dwell_action = action_type
        log.info("Active dwell action set to: %s", action_type)

        payload = {"type": "ack", "status": "ok", "action_type": action_type}
        if msg_id is not None:
            payload["id"] = msg_id
        try:
            await ws.send_json(payload)
        except Exception as exc:
            log.debug("Failed to send ack: %s", exc)

    # ---------------------------------------------------------------------- #
    # set_feature_toggle — update feature enabled/disabled state
    # ---------------------------------------------------------------------- #

    async def _handle_set_feature_toggle(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Handle set_feature_toggle messages. Validates feature name and forwards to FusionEngine."""
        msg_id = msg.get("id")
        feature = msg.get("feature")
        enabled = msg.get("enabled")

        # Validate feature name
        if feature not in self.VALID_FEATURES:
            payload: dict = {
                "type": "ack",
                "id": msg_id,
                "status": "error",
                "error": f"unknown feature: {feature!r}",
            }
            try:
                await ws.send_json(payload)
            except Exception as exc:
                log.debug("Failed to send ack: %s", exc)
            return

        # Coerce enabled to bool
        enabled = bool(enabled)

        # Forward to FusionEngine
        if self._fusion:
            self._fusion.set_feature_toggle(feature, enabled)

        # Respond with success ack
        payload = {
            "type": "ack",
            "id": msg_id,
            "status": "ok",
            "feature": feature,
            "enabled": enabled,
        }
        try:
            await ws.send_json(payload)
        except Exception as exc:
            log.debug("Failed to send ack: %s", exc)

    # ---------------------------------------------------------------------- #
    # Response helpers
    # ---------------------------------------------------------------------- #

    async def _ack(
        self,
        ws: web.WebSocketResponse,
        msg_id: Any,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        payload: dict = {"type": "ack", "id": msg_id, "status": status}
        if error:
            payload["error"] = error
        try:
            await ws.send_json(payload)
        except Exception as exc:
            log.debug("Failed to send ack: %s", exc)

    # ---------------------------------------------------------------------- #
    # iPad structured log forwarding
    # ---------------------------------------------------------------------- #

    # Level mapping: iPad AppLogger levels → Python logging levels
    _IPAD_LOG_LEVELS: dict = {
        "debug":   logging.DEBUG,
        "info":    logging.INFO,
        "warning": logging.WARNING,
        "error":   logging.ERROR,
        "fault":   logging.CRITICAL,
    }

    # Whitelist for the iPad-controlled `subsystem` field. The full string is
    # interpolated into a Python logger name; without bounding it, a malicious
    # client on the LAN could spam loggerDict and grow unbounded. Length-capped
    # to 32, character class to ASCII identifier-safe.
    _IPAD_SUBSYSTEM_RE = re.compile(r"^[A-Za-z0-9_.]{1,32}$")

    def _sanitize_subsystem(self, subsystem: object) -> str:
        if isinstance(subsystem, str) and self._IPAD_SUBSYSTEM_RE.match(subsystem):
            return subsystem
        return "unknown"

    def _on_ipad_log_task_done(self, task: asyncio.Task) -> None:
        self._ipad_log_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("ipad_log DB write failed: %s", exc)

    async def _handle_ipad_log(self, msg: dict) -> None:
        """Handle an ipad_log batch message.

        Routes each entry to a Python logger named 'ipad.<subsystem>' so iPad
        events appear in the main log file alongside PC-side events. warning+
        entries are also persisted to the ipad_logs AgentDB table for soak
        test post-analysis.
        """
        entries = msg.get("entries")
        if not entries or not isinstance(entries, list):
            return

        db_entries: list = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            level_str = entry.get("level", "info")
            subsystem = self._sanitize_subsystem(entry.get("subsystem", "unknown"))
            text = entry.get("msg", "")
            py_level = self._IPAD_LOG_LEVELS.get(level_str, logging.INFO)
            logging.getLogger(f"ipad.{subsystem}").log(py_level, "%s", text)
            if py_level >= logging.WARNING:
                db_entries.append(entry)

        if db_entries and self._agent_db and self._session_id >= 0:
            trace_id: Optional[str] = None
            if (
                self._active_trace_id
                and time.time() - self._active_trace_ts < self._TRACE_WINDOW_S
            ):
                trace_id = self._active_trace_id
            task = asyncio.create_task(
                self._agent_db.log_ipad_events(
                    self._session_id, db_entries, trace_id=trace_id
                )
            )
            self._ipad_log_tasks.add(task)
            task.add_done_callback(self._on_ipad_log_task_done)

    async def _send_status(self, ws: web.WebSocketResponse) -> None:
        from tools import windows as win_tools
        import pyautogui

        try:
            active = await asyncio.to_thread(win_tools.get_active_window)
            cx, cy = pyautogui.position()
            payload = {
                "type": "status",
                "active_window": active.get("title"),
                "cursor": {"x": cx, "y": cy},
                "ts": time.time(),
            }
            await ws.send_json(payload)
        except Exception as exc:
            log.debug("Failed to send status: %s", exc)

    async def broadcast_json(self, payload: dict) -> None:
        """Send a JSON message to all connected iPad clients."""
        dead: set = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    def _notify_mic_state(self, muted: bool) -> None:
        """Sync callback from WhisperStream.set_muted — schedule a broadcast.

        set_muted() is synchronous and may be called from either the bridge's
        mic_mute handler or the coordinator's voice path; both run on the bridge
        event loop, so create_task is safe. Guard for the no-loop case.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.broadcast_mic_state(muted))

    async def broadcast_mic_state(self, muted: bool) -> None:
        """Push the current mic mute state to all connected iPad clients."""
        await self.broadcast_json({"type": "mic_state", "muted": bool(muted)})

    # ---------------------------------------------------------------------- #
    # A2UI — agent-generated declarative touch surfaces (see core/a2ui.py)
    # ---------------------------------------------------------------------- #

    async def send_a2ui_surface(self, surface: dict) -> bool:
        """Validate and broadcast an A2UI surface to connected iPads.

        Returns False (and sends nothing) if the payload fails schema
        validation — the caller should then fall back to voice, so the renderer
        never receives a malformed surface (whitepaper best practice).
        """
        from core import a2ui

        errs = a2ui.validate_surface(surface)
        if errs:
            log.warning("a2ui: refusing to send invalid surface: %s", "; ".join(errs))
            return False
        log.info("a2ui: sending surface %s (%d components)",
                 surface.get("surface_id"), len(surface.get("components", [])))
        await self.broadcast_json(surface)
        return True

    async def clear_a2ui_surface(self, surface_id: str) -> None:
        """Dismiss a live surface on the iPad (agent resolved it another way)."""
        await self.broadcast_json({"type": "a2ui_clear", "surface_id": surface_id})

    def register_a2ui_surface(self, surface_id: str) -> "asyncio.Future":
        """Create a Future resolved when the user taps on ``surface_id``.

        Used by the CLARIFY path (Phase 2) to await an enumerable answer; the
        approval gate (Phase 1) does not use this — it writes the response file.
        """
        loop = asyncio.get_event_loop()
        fut: "asyncio.Future" = loop.create_future()
        self._a2ui_pending[surface_id] = fut
        return fut

    async def _handle_a2ui_event(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """iPad → PC: the user tapped a control on an A2UI surface.

        Two resolution paths, both reusing EXISTING decision plumbing:
          * event == "approval"  → write the canonical token to the shared
            ``~/.claude/approval/response`` file (identical to a voice verdict).
          * otherwise            → resolve a pending surface Future if one is
            registered (enumerable CLARIFY).
        """
        surface_id = msg.get("surface_id", "")
        event = msg.get("event", "")
        value = msg.get("value")
        values = msg.get("values") or {}
        log.info("a2ui_event: surface=%s event=%s value=%r", surface_id, event, value)

        if event == "approval":
            # Parallel input to the voice approval gate. Deny on anything that
            # isn't an explicit "approve" — fail safe, matching the voice path.
            verdict = "approve" if value == "approve" else "deny"
            try:
                self._approval_dir.mkdir(parents=True, exist_ok=True)
                (self._approval_dir / "response").write_text(verdict, encoding="utf-8")
                log.info("a2ui_event: approval response %s ← tap", verdict)
            except Exception as exc:
                log.warning("a2ui_event: could not write approval response: %s", exc)
            await self._ack(ws, msg.get("id"), "ok")
            return

        if event == "click_target" and value and self._coordinator is not None:
            # The tapped element's pixel center ("x,y") → a coordinate-precise
            # CLICK via the touch-bypass path (no LLM, no re-grounding). Routing
            # through the coordinator also clears the pending clarification.
            try:
                xs, ys = str(value).split(",", 1)
                x, y = int(float(xs)), int(float(ys))
                cmd = Command(text="click element", action="CLICK",
                              source="touch", gaze_coords=(x, y))
                asyncio.create_task(self._coordinator.route(cmd))
            except Exception as exc:
                log.debug("a2ui_event: bad click_target value %r: %s", value, exc)
            await self._ack(ws, msg.get("id"), "ok")
            return

        fut = self._a2ui_pending.pop(surface_id, None)
        if fut is not None and not fut.done():
            fut.set_result({"event": event, "value": value, "values": values})
        elif value and self._coordinator is not None:
            # CLARIFY answer: re-enter the pipeline as a voice-equivalent command
            # so the existing pending-clarification context resolves it exactly
            # like a spoken reply (action is a placeholder; the LLM reclassifies).
            try:
                answer = Command(text=str(value), action="DICTATE", source="voice")
                asyncio.create_task(self._coordinator.route(answer))
            except Exception as exc:
                log.debug("a2ui_event: could not route clarify answer: %s", exc)
        await self._ack(ws, msg.get("id"), "ok")

    async def send_recalibration_request(
        self, reason: str = "voice_clarity", degradation_pct: float = 0.0
    ) -> None:
        """Ask the iPad to start a quick voice re-calibration session."""
        payload = {
            "type":            "recalibration_request",
            "reason":          reason,
            "degradation_pct": degradation_pct,
        }
        log.info("ipad_bridge: sending recalibration_request (reason=%s)", reason)
        await self.broadcast_json(payload)

    async def _send_handwriting_result(
        self,
        ws: web.WebSocketResponse,
        msg_id: Any,
        result: dict,
    ) -> None:
        payload = {"type": "handwriting_result", "id": msg_id}
        payload.update(result)
        try:
            await ws.send_json(payload)
        except Exception as exc:
            log.debug("Failed to send handwriting_result: %s", exc)

    async def _send_screenshot(
        self,
        ws: web.WebSocketResponse,
        msg_id: Any,
        image_base64: str,
    ) -> None:
        try:
            await ws.send_json({
                "type": "screenshot",
                "id": msg_id,
                "image": image_base64,
                "mime": "image/png",
            })
        except Exception as exc:
            log.debug("Failed to send screenshot: %s", exc)

    # ---------------------------------------------------------------------- #
    # mDNS
    # ---------------------------------------------------------------------- #

    async def _start_mdns(self) -> None:
        if not _ZEROCONF_AVAILABLE:
            return
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        info = ServiceInfo(
            "_desktop-agent._tcp.local.",
            f"{hostname}._desktop-agent._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={"version": "1", "name": "Personal Desktop Agent"},
        )
        try:
            self._zeroconf = AsyncZeroconf()
            await self._zeroconf.async_register_service(info, allow_name_change=True)
            log.info("mDNS: %s._desktop-agent._tcp.local. → %s:%d",
                     hostname, local_ip, self.port)
        except Exception as exc:
            log.warning("mDNS registration failed (non-fatal): %r", exc, exc_info=True)
            if self._zeroconf:
                await self._zeroconf.async_close()
            self._zeroconf = None

    async def _stop_mdns(self) -> None:
        if self._zeroconf:
            await self._zeroconf.async_close()

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def run(self, no_mdns: bool = False) -> None:
        await self._resolve_screen_size()

        if not no_mdns:
            await self._start_mdns()

        # Prevent Windows from sleeping while bridge is active
        self._prevent_sleep()

        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/health", self._health_handler)

        # Serve the web client (iPad Safari fallback)
        web_client_dir = Path(__file__).parent / "web_client"
        if web_client_dir.exists():
            app.router.add_static("/", web_client_dir, show_index=True)
            log.info("Web client served from %s", web_client_dir)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port, reuse_address=True)
        await site.start()

        log.info("Bridge listening on %s:%d  (ws://%s:%d/ws)", self.host, self.port, self.host, self.port)
        log.info("Web client: http://%s:%d/", self.host, self.port)
        log.info("iPad pairing token: %s", self._token)
        log.info("  → enter this token in the iPad app, or connect with "
                 "ws://%s:%d/ws?token=%s", self.host, self.port, self._token)
        self._print_qr()

        try:
            await asyncio.Event().wait()
        finally:
            self._allow_sleep()
            await self._stop_mdns()
            await runner.cleanup()

    def _prevent_sleep(self) -> None:
        """Prevent Windows from sleeping while the bridge is running.
        Uses SetThreadExecutionState to keep the system awake (display can still turn off)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            log.info("Sleep prevention enabled — PC will stay awake while bridge runs")
        except Exception as exc:
            log.warning("Could not prevent sleep: %s", exc)

    def _allow_sleep(self) -> None:
        """Re-enable normal sleep behavior."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            log.info("Sleep prevention disabled")
        except Exception:
            pass

    @staticmethod
    async def _health_handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "desktop-agent-bridge"})

    @staticmethod
    def _detect_lan_ip() -> str:
        """Return the LAN IP the OS would use to reach the default gateway.

        Falls back to socket.gethostbyname(hostname) and finally 'localhost' if
        neither works. The default-route trick is necessary on Windows machines
        with active VPN / Tailscale / WireGuard / Hyper-V interfaces, where
        gethostbyname(gethostname()) commonly returns the VPN address (10.x.x.x
        or similar) instead of the actual LAN IP the iPad needs to reach.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # No packet is actually sent; this just asks the OS which
                # interface it would use to route to a public address.
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
            finally:
                s.close()
        except Exception:
            pass
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "localhost"

    def _print_qr(self) -> None:
        """Print connection info to the terminal (QR code if qrcode installed)."""
        if self.host != "0.0.0.0":
            local_ip = self.host
        else:
            local_ip = self._detect_lan_ip()
        url = f"ws://{local_ip}:{self.port}/ws"
        print(f"\n  Connect iPad to:  {url}\n")
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="iPad Bridge WebSocket server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-mdns", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = IPadBridge(port=args.port)
    try:
        asyncio.run(bridge.run(no_mdns=args.no_mdns))
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
