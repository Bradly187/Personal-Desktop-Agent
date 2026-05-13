"""iPad Bridge — WebSocket server for the Personal Desktop Agent

Listens on port 8765 for messages from the iPad app and dispatches them
to FusionEngine, CommandExecutor, or directly to pyautogui.

Message types — 13 total:
  touch_command     →  CommandExecutor (action routing; priority 1)
  trackpad          →  direct pyautogui (mouse/scroll; no LLM)
  handwriting_image →  pix2tex OCR → handwriting_result reply
  tilt              →  FusionEngine.on_tilt() (when wired)
  tilt_tap          →  FusionEngine.on_touch() (when wired)
  gaze              →  FusionEngine.on_gaze() (when wired)
  gaze_dwell        →  FusionEngine.on_gaze_dwell() (when wired)
  head_pose         →  FusionEngine.on_head() (when wired)
  keyword           →  FusionEngine.on_keyword() (when wired)
  sound_action      →  FusionEngine.on_sound_action() (when wired)
  depth_frame       →  LiDARReceiver.on_depth_frame() (when wired)
  camera_frame      →  GestureProcessor.on_camera_frame() (when wired)
  audio_stream      →  WhisperStream.on_audio_chunk() (VAD + Whisper transcription)

Usage:
  python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

mDNS advertisement: _desktop-agent._tcp.local. on the chosen port
so the iPad app can discover the PC without manual IP entry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

# Make mcp_server/tools importable without install
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent / "mcp_server"))

from command_executor import Command, CommandExecutor

log = logging.getLogger("ipad_bridge")

# TYPE_CHECKING imports — avoid circular deps at runtime
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from fusion_engine import FusionEngine
    from gesture_processor import GestureProcessor
    from lidar_receiver import LiDARReceiver
    from whisper_stream import WhisperStream

# ---------------------------------------------------------------------------
# Optional mDNS — graceful if zeroconf unavailable
# ---------------------------------------------------------------------------

try:
    from zeroconf import ServiceInfo, Zeroconf
    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False
    log.warning("zeroconf not installed — mDNS discovery disabled. "
                "Install with: pip install zeroconf")


# ---------------------------------------------------------------------------
# iPad Bridge
# ---------------------------------------------------------------------------

class IPadBridge:
    def __init__(self, port: int = 8765):
        self.port = port

        # Screen dimensions (resolved lazily at start)
        self._screen_w: int = 1920
        self._screen_h: int = 1080

        # Direct executor for Phase 1 touch/trackpad commands
        self._executor = CommandExecutor()

        # Phase 2+ pipeline components (wired by main.py)
        self._fusion: Optional["FusionEngine"] = None
        self._lidar: Optional["LiDARReceiver"] = None
        self._gesture: Optional["GestureProcessor"] = None
        self._whisper: Optional["WhisperStream"] = None

        self._clients: set[web.WebSocketResponse] = set()
        self._zeroconf: Any = None

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

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        peer = request.remote
        log.info("Client connected: %s", peer)
        self._clients.add(ws)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
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
        # Sensor streams — dispatched to FusionEngine / Phase 3 components
        # No ack sent for high-frequency streams (tilt, gaze, head_pose).
        # All sensor handlers are wrapped in try/except to prevent malformed
        # data from crashing the bridge.
        # ------------------------------------------------------------------ #
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
            if self._fusion:
                cmd = Command(text="tilt_tap", action="CLICK", source="touch")
                self._fusion.on_touch(cmd)
            return

        if msg_type == "gaze":
            try:
                if self._fusion:
                    x = float(msg.get("x", 0.5))
                    y = float(msg.get("y", 0.5))
                    conf = float(msg.get("confidence", 0.0))
                    self._fusion.on_gaze(x, y, conf)
            except (ValueError, TypeError) as exc:
                log.debug("Bad gaze data: %s", exc)
            return

        if msg_type == "gaze_dwell":
            try:
                if self._fusion:
                    x = float(msg.get("x", 0.5))
                    y = float(msg.get("y", 0.5))
                    self._fusion.on_gaze_dwell(x, y)
            except (ValueError, TypeError) as exc:
                log.debug("Bad gaze_dwell data: %s", exc)
            return

        if msg_type == "head_pose":
            try:
                if self._fusion:
                    pitch = float(msg.get("pitch", 0.0))
                    yaw   = float(msg.get("yaw", 0.0))
                    self._fusion.on_head(pitch, yaw)
            except (ValueError, TypeError) as exc:
                log.debug("Bad head_pose data: %s", exc)
            return

        if msg_type == "keyword":
            if self._fusion:
                word = str(msg.get("word", ""))
                conf = float(msg.get("confidence", 1.0))
                self._fusion.on_keyword(word, conf)
            return

        if msg_type == "sound_action":
            if self._fusion:
                sound = str(msg.get("sound", ""))
                conf  = float(msg.get("confidence", 1.0))
                self._fusion.on_sound_action(sound, conf)
            return

        if msg_type == "depth_frame":
            if self._lidar:
                self._lidar.on_depth_frame(msg)
            else:
                log.debug("depth_frame received but LiDARReceiver not wired")
            return

        if msg_type == "camera_frame":
            if self._gesture:
                gesture_cmd = self._gesture.on_camera_frame(msg)
                if gesture_cmd and self._fusion:
                    self._fusion.on_gesture(gesture_cmd)
            else:
                log.debug("camera_frame received but GestureProcessor not wired")
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

        log.warning("Unknown message type: %s", msg_type)
        await self._ack(ws, msg_id, "error", f"unknown type: {msg_type}")

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

    def _start_mdns(self) -> None:
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
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(info)
            log.info("mDNS: %s._desktop-agent._tcp.local. → %s:%d",
                     hostname, local_ip, self.port)
        except Exception as exc:
            log.warning("mDNS registration failed (non-fatal): %s", exc)
            self._zeroconf = None

    def _stop_mdns(self) -> None:
        if self._zeroconf:
            self._zeroconf.close()

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def run(self, no_mdns: bool = False) -> None:
        await self._resolve_screen_size()

        if not no_mdns:
            self._start_mdns()

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
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        log.info("Bridge listening on :%d  (ws://0.0.0.0:%d/ws)", self.port, self.port)
        log.info("Web client: http://0.0.0.0:%d/", self.port)
        self._print_qr()

        try:
            await asyncio.Event().wait()
        finally:
            self._allow_sleep()
            self._stop_mdns()
            await runner.cleanup()

    def _prevent_sleep(self) -> None:
        """Prevent Windows from sleeping while the bridge is running.
        Uses SetThreadExecutionState to keep the system awake (display can still turn off)."""
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

    def _print_qr(self) -> None:
        """Print connection info to the terminal (QR code if qrcode installed)."""
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "localhost"
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
