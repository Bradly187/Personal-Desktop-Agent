"""FusionEngine — 10-level priority sensor fusion at 60 Hz.

Receives sensor events from IPadBridge and on each tick emits at most one
Command to HybridCoordinator, or moves the cursor directly for tilt/head
events (which bypass the LLM entirely).

Priority order (highest → lowest):
  1  touch       — iPad CommandPad tap, bypass all gates
  2  sound       — AVFoundation mouth sound, bypass all gates
  3  gaze_dwell  — ARKit stable gaze timer fired, bypass all gates
  4  multimodal  — gaze stable + voice "click", bypass all gates
  5  multimodal  — gaze stable + POINT gesture
  6  tilt        — Core Motion tilt → pyautogui.moveRel (no Command)
  7  head        — ARKit head pose → pyautogui.moveRel (no Command)
  8  gesture     — MediaPipe gesture command
  9  voice_local — Speech Framework keyword (skip Gate 1)
  10 voice       — PC Whisper transcription (full 4-gate)

See diagrams/06-fusion-routing.md for the full decision flowchart.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Deque, Optional

from command_executor import Command

if TYPE_CHECKING:
    from hybrid_coordinator import HybridCoordinator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class FusionConfig:
    tick_hz: float = 60.0
    dwell_duration_s: float = 1.0          # seconds stable gaze required for dwell
    gaze_stability_pct: float = 0.04       # max spread as fraction of screen diagonal
    gaze_conf_min: float = 0.55            # minimum ARKit confidence to accept gaze
    gaze_buffer_frames: int = 30           # rolling window size for stability check
    tilt_dead_zone: float = 0.02           # rad/s below which tilt is ignored
    tilt_sensitivity: float = 300.0        # pixels moved per rad/s
    head_sensitivity: float = 80.0         # pixels moved per degree
    sound_cooldown_s: float = 0.5          # suppress duplicate sound actions


# ---------------------------------------------------------------------------
# Gaze stability buffer
# ---------------------------------------------------------------------------

@dataclass
class _GazeSample:
    x: float   # normalized [0, 1]
    y: float
    ts: float  # time.monotonic()


class _GazeBuffer:
    def __init__(self, maxlen: int, stability_pct: float, conf_min: float) -> None:
        self._buf: Deque[_GazeSample] = deque(maxlen=maxlen)
        self._stability_pct = stability_pct
        self._conf_min = conf_min

    def update(self, x: float, y: float, conf: float) -> None:
        if conf >= self._conf_min:
            self._buf.append(_GazeSample(x, y, time.monotonic()))

    def stable_centroid(self, screen_diag: float) -> Optional[tuple[float, float]]:
        """Return (x, y) centroid if gaze is stable, else None."""
        recent = [s for s in self._buf if time.monotonic() - s.ts < 0.3]
        if len(recent) < 5:
            return None
        xs = [s.x for s in recent]
        ys = [s.y for s in recent]
        spread_norm = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        # Convert to pixels-like measure: spread_norm * screen_diag (in normalized units, diag=√2)
        if spread_norm > self._stability_pct:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))


# ---------------------------------------------------------------------------
# FusionEngine
# ---------------------------------------------------------------------------

class FusionEngine:
    def __init__(
        self,
        screen_width: int,
        screen_height: int,
        config: FusionConfig | None = None,
    ) -> None:
        self._w = screen_width
        self._h = screen_height
        self._diag = math.hypot(screen_width, screen_height)
        self._cfg = config or FusionConfig()

        # --- Input slots (cleared after consumption) ---
        self._touch: Optional[Command] = None
        self._sound: Optional[Command] = None
        self._gaze_dwell: Optional[tuple[float, float]] = None   # norm (x, y)
        self._gesture: Optional[Command] = None
        self._voice_local: Optional[str] = None                  # keyword text
        self._voice: Optional[Command] = None
        self._tilt: Optional[tuple[float, float]] = None          # (rx, ry) rad/s
        self._head: Optional[tuple[float, float]] = None          # (pitch, yaw) degrees

        # --- Gaze state ---
        self._gaze_buf = _GazeBuffer(
            self._cfg.gaze_buffer_frames,
            self._cfg.gaze_stability_pct,
            self._cfg.gaze_conf_min,
        )
        self._dwell_start: Optional[float] = None
        self._dwell_region: Optional[tuple[float, float]] = None

        # --- Sound debounce ---
        self._last_sound_ts: float = 0.0

        # --- Wired coordinator ---
        self._coordinator: Optional["HybridCoordinator"] = None
        self._running = False

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_coordinator(self, coordinator: "HybridCoordinator") -> None:
        self._coordinator = coordinator

    # ---------------------------------------------------------------------- #
    # Event callbacks — called from IPadBridge (via asyncio)
    # ---------------------------------------------------------------------- #

    def on_touch(self, cmd: Command) -> None:
        self._touch = cmd

    def on_sound_action(self, sound: str, conf: float) -> None:
        now = time.monotonic()
        if now - self._last_sound_ts < self._cfg.sound_cooldown_s:
            return
        self._last_sound_ts = now
        self._sound = Command(
            text=f"sound:{sound}",
            action="CLICK",   # default; coordinator maps sound → action
            source="sound_action",
            gesture_confidence=conf,
        )

    def on_gaze(self, x: float, y: float, conf: float) -> None:
        self._gaze_buf.update(x, y, conf)

    def on_gaze_dwell(self, x: float, y: float) -> None:
        self._gaze_dwell = (x, y)

    def on_tilt(self, rx: float, ry: float) -> None:
        self._tilt = (rx, ry)

    def on_head(self, pitch: float, yaw: float) -> None:
        self._head = (pitch, yaw)

    def on_keyword(self, word: str, conf: float) -> None:
        self._voice_local = word

    def on_gesture(self, cmd: Command) -> None:
        self._gesture = cmd

    def on_voice(self, cmd: Command) -> None:
        self._voice = cmd

    # ---------------------------------------------------------------------- #
    # 60 Hz tick
    # ---------------------------------------------------------------------- #

    async def _tick(self) -> None:
        import pyautogui

        # Rule 1 — Touch (bypass all gates)
        if self._touch:
            cmd, self._touch = self._touch, None
            await self._emit(cmd)
            return

        # Rule 2 — Sound action (bypass all gates)
        if self._sound:
            cmd, self._sound = self._sound, None
            await self._emit(cmd)
            return

        # Rule 3 — Gaze dwell (bypass all gates)
        if self._gaze_dwell:
            nx, ny = self._gaze_dwell
            self._gaze_dwell = None
            self._dwell_start = None
            cmd = Command(
                text="gaze dwell click",
                action="CLICK",
                source="gaze_dwell",
                gaze_coords=(int(nx * self._w), int(ny * self._h)),
            )
            await self._emit(cmd)
            return

        # Gaze stability — used by Rules 4 & 5
        stable = self._gaze_buf.stable_centroid(self._diag)

        # Rule 4 — Gaze stable + voice keyword "click" (bypass all gates)
        if stable and self._voice_local and self._voice_local.lower().strip() == "click":
            self._voice_local = None
            cmd = Command(
                text="gaze voice click",
                action="CLICK",
                source="multimodal",
                gaze_coords=(int(stable[0] * self._w), int(stable[1] * self._h)),
            )
            await self._emit(cmd)
            return

        # Rule 5 — Gaze stable + POINT gesture
        if stable and self._gesture and self._gesture.params.get("gesture") == "POINT":
            self._gesture = None
            cmd = Command(
                text="gaze gesture click",
                action="CLICK",
                source="multimodal",
                gaze_coords=(int(stable[0] * self._w), int(stable[1] * self._h)),
            )
            await self._emit(cmd)
            return

        # Rule 6 — Tilt navigation (direct to pyautogui, no Command, no LLM)
        if self._tilt:
            rx, ry = self._tilt
            self._tilt = None
            dz = self._cfg.tilt_dead_zone
            if abs(rx) > dz or abs(ry) > dz:
                dx = int(ry * self._cfg.tilt_sensitivity)
                dy = int(-rx * self._cfg.tilt_sensitivity)
                await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
            return

        # Rule 7 — Head tracking (direct to pyautogui, no Command, no LLM)
        if self._head:
            pitch, yaw = self._head
            self._head = None
            dx = int(yaw * self._cfg.head_sensitivity)
            dy = int(-pitch * self._cfg.head_sensitivity)
            if dx or dy:
                await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
            return

        # Rule 8 — Gesture (full 4-gate)
        if self._gesture:
            cmd, self._gesture = self._gesture, None
            await self._emit(cmd)
            return

        # Rule 9 — On-device voice keyword, skip Gate 1
        if self._voice_local:
            word, self._voice_local = self._voice_local, None
            cmd = Command(text=word, action="DICTATE", source="voice_local")
            await self._emit(cmd)
            return

        # Rule 10 — PC-transcribed voice, full 4-gate
        if self._voice:
            cmd, self._voice = self._voice, None
            await self._emit(cmd)

    async def _emit(self, cmd: Command) -> None:
        if self._coordinator:
            await self._coordinator.route(cmd)
        else:
            log.warning("FusionEngine: no coordinator set — dropping %r", cmd)

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def run(self) -> None:
        self._running = True
        interval = 1.0 / self._cfg.tick_hz
        log.info("FusionEngine running at %.0f Hz", self._cfg.tick_hz)
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick()
            except Exception as exc:
                log.error("FusionEngine tick error: %s", exc)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))

    def stop(self) -> None:
        self._running = False
        log.info("FusionEngine stopped")
