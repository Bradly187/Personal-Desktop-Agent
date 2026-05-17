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
    tilt_dead_zone: float = 0.05           # rad/s below which tilt is ignored
    tilt_sensitivity: float = 200.0        # pixels moved per rad/s
    head_sensitivity: float = 80.0         # pixels moved per degree
    sound_cooldown_s: float = 0.5          # suppress duplicate sound actions

    # Edge scroll
    edge_scroll_zone_pct: float = 0.08     # 8% from each edge
    edge_scroll_delay_ms: int = 500        # activation delay before scrolling starts
    edge_scroll_min_speed: int = 1         # scroll units/tick at inner boundary
    edge_scroll_max_speed: int = 10        # scroll units/tick at screen edge

    # Gaze-to-cursor
    gaze_cursor_ema_alpha: float = 0.12      # EMA smoothing factor for cursor position
    gaze_cursor_max_jump_pct: float = 0.05   # max 5% of screen diagonal per tick
    gaze_cursor_conf_min: float = 0.55       # minimum confidence to move cursor
    gaze_cursor_lost_timeout_s: float = 0.5  # hold position after gaze lost this long

    # Tilt position (absolute-mapping mode)
    tilt_pos_alpha: float = 0.4              # EMA smoothing factor (0=no smoothing, 1=instant)

    def __post_init__(self) -> None:
        if not (0.02 <= self.edge_scroll_zone_pct <= 0.20):
            raise ValueError(
                f"edge_scroll_zone_pct must be in [0.02, 0.20], got {self.edge_scroll_zone_pct}"
            )


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
        self._last_conf: float = 0.0  # confidence of most recent update (even if below threshold)

    def update(self, x: float, y: float, conf: float) -> None:
        self._last_conf = conf
        if conf >= self._conf_min:
            self._buf.append(_GazeSample(x, y, time.monotonic()))

    def latest(self) -> Optional[_GazeSample]:
        """Return the most recent gaze sample, or None if buffer is empty."""
        return self._buf[-1] if self._buf else None

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
    # Valid dwell action types for gaze dwell routing
    VALID_DWELL_ACTIONS: set[str] = {
        "left_click", "right_click", "double_click", "drag_start", "drag_end"
    }

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
        self._gaze_dwell_action_type: str = "left_click"         # action type for pending dwell
        self._gesture: Optional[Command] = None
        self._voice_local: Optional[str] = None                  # keyword text
        self._voice: Optional[Command] = None
        self._tilt: Optional[tuple[float, float]] = None          # (rx, ry) rad/s
        self._tilt_ema_x: float = 0.0                             # EMA-smoothed tilt X
        self._tilt_ema_y: float = 0.0                             # EMA-smoothed tilt Y
        self._tilt_accum_x: float = 0.0                           # sub-pixel accumulator X
        self._tilt_accum_y: float = 0.0                           # sub-pixel accumulator Y
        self._head: Optional[tuple[float, float]] = None          # (pitch, yaw) degrees

        # --- Tilt position (absolute positioning from iPad position-mapped mode) ---
        self._tilt_position: Optional[tuple[float, float]] = None  # (x, y) normalized [0,1]
        self._tilt_pos_ema_x: float = 0.5                          # EMA-smoothed position X
        self._tilt_pos_ema_y: float = 0.5                          # EMA-smoothed position Y
        self._tilt_pos_initialized: bool = False                   # first sample initializes EMA

        # --- Gaze state ---
        self._gaze_buf = _GazeBuffer(
            self._cfg.gaze_buffer_frames,
            self._cfg.gaze_stability_pct,
            self._cfg.gaze_conf_min,
        )
        self._dwell_start: Optional[float] = None
        self._dwell_region: Optional[tuple[float, float]] = None

        # --- Gaze-to-cursor state ---
        self._gaze_cursor_last: Optional[tuple[int, int]] = None      # last smoothed pixel pos
        self._gaze_cursor_ema: Optional[tuple[float, float]] = None   # EMA state (norm coords)
        self._gaze_cursor_last_seen: float = 0.0                      # monotonic time of last valid gaze

        # --- Drag state (PC-side tracking for safety) ---
        self._drag_active: bool = False
        self._drag_start_time: float | None = None     # monotonic time drag_start fired
        self._drag_safety_timeout_s: float = 30.0      # auto-release after 30s with no drag_end

        # --- Edge scroll state ---
        self._edge_scroll_active: bool = False
        self._edge_scroll_start: float | None = None   # monotonic time gaze entered zone
        self._edge_scroll_direction: str | None = None  # "up"/"down"/"left"/"right"

        # --- Sound debounce ---
        self._last_sound_ts: float = 0.0

        # --- Feature toggles (synced from iPad via bridge) ---
        self._feature_toggles: dict[str, bool] = {
            "gaze_dwell_click": True,
            "gaze_dwell_right_click": True,
            "gaze_dwell_double_click": True,
            "gaze_dwell_drag": True,
            "edge_scroll": True,
            "gaze_cursor_mode": True,
        }

        # --- Wired coordinator ---
        self._coordinator: Optional["HybridCoordinator"] = None
        self._running = False

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_coordinator(self, coordinator: "HybridCoordinator") -> None:
        self._coordinator = coordinator

    # ---------------------------------------------------------------------- #
    # Feature toggle management
    # ---------------------------------------------------------------------- #

    # Valid feature toggle keys
    VALID_FEATURES: set[str] = {
        "gaze_dwell_click", "gaze_dwell_right_click", "gaze_dwell_double_click",
        "gaze_dwell_drag", "edge_scroll", "gaze_cursor_mode",
    }

    def set_feature_toggle(self, feature: str, enabled: bool) -> None:
        """Update a feature toggle. Called by IPadBridge on set_feature_toggle messages."""
        if feature in self.VALID_FEATURES:
            self._feature_toggles[feature] = enabled
            log.info("Feature toggle: %s = %s", feature, enabled)
        else:
            log.warning("Unknown feature toggle: %s", feature)

    # Mapping from dwell action_type to feature toggle key
    _ACTION_TYPE_TO_TOGGLE: dict[str, str] = {
        "left_click": "gaze_dwell_click",
        "right_click": "gaze_dwell_right_click",
        "double_click": "gaze_dwell_double_click",
        "drag_start": "gaze_dwell_drag",
        "drag_end": "gaze_dwell_drag",
    }

    def _action_type_to_toggle(self, action_type: str) -> str | None:
        """Map a dwell action_type to its corresponding feature toggle key.

        Returns the toggle key string, or None if no mapping exists.
        """
        return self._ACTION_TYPE_TO_TOGGLE.get(action_type)

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

    def on_gaze_dwell(self, x: float, y: float, action_type: str = "left_click") -> None:
        self._gaze_dwell = (x, y)
        self._gaze_dwell_action_type = action_type

    def on_tilt(self, rx: float, ry: float) -> None:
        self._tilt = (rx, ry)

    def on_tilt_position(self, x: float, y: float) -> None:
        """Receive absolute position from iPad tilt sensor (position-mapped mode)."""
        self._tilt_position = (x, y)

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
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0

        tick_start = time.monotonic()

        # --- Drag safety timeout: auto-release if drag_start without drag_end for 30s ---
        if self._drag_active and self._drag_start_time is not None:
            elapsed = tick_start - self._drag_start_time
            if elapsed > self._drag_safety_timeout_s:
                log.error(
                    "Drag safety timeout: no drag_end received in %.1fs — auto-releasing mouse",
                    elapsed,
                )
                auto_release_cmd = Command(
                    text="drag safety timeout auto-release",
                    action="MOUSEUP",
                    source="gaze_dwell",
                    params={},
                )
                self._drag_active = False
                self._drag_start_time = None
                await self._emit(auto_release_cmd)
                return

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
            action_type = self._gaze_dwell_action_type
            self._gaze_dwell = None
            self._dwell_start = None

            # Validate action type
            if action_type not in self.VALID_DWELL_ACTIONS:
                log.warning(
                    "Unrecognized gaze dwell action_type: %r — discarding event", action_type
                )
                return

            # Check feature toggle — discard if the corresponding feature is disabled
            toggle_key = self._action_type_to_toggle(action_type)
            if toggle_key and not self._feature_toggles.get(toggle_key, True):
                log.debug(
                    "Gaze dwell action %r discarded — feature %r is disabled",
                    action_type, toggle_key,
                )
                return

            # When gaze-to-cursor mode is active, use EMA-smoothed position
            # instead of raw gaze coordinates for the dwell click target.
            # The dwell timer fires normally; only the landing position changes.
            if (self._feature_toggles.get("gaze_cursor_mode", False)
                    and self._gaze_cursor_ema is not None):
                nx, ny = self._gaze_cursor_ema

            # Clamp coordinates to [0.0, 1.0] and compute pixel coords
            cx = max(0.0, min(1.0, nx))
            cy = max(0.0, min(1.0, ny))
            px_x = round(cx * self._w)
            px_y = round(cy * self._h)

            # Route action type to appropriate Command
            if action_type == "left_click":
                cmd = Command(
                    text="gaze dwell click",
                    action="CLICK",
                    source="gaze_dwell",
                    params={},
                    gaze_coords=(px_x, px_y),
                )
            elif action_type == "right_click":
                cmd = Command(
                    text="gaze dwell right click",
                    action="CLICK",
                    source="gaze_dwell",
                    params={"button": "right"},
                    gaze_coords=(px_x, px_y),
                )
            elif action_type == "double_click":
                cmd = Command(
                    text="gaze dwell double click",
                    action="CLICK",
                    source="gaze_dwell",
                    params={"clicks": "2"},
                    gaze_coords=(px_x, px_y),
                )
            elif action_type == "drag_start":
                cmd = Command(
                    text="gaze dwell drag start",
                    action="MOUSEDOWN",
                    source="gaze_dwell",
                    params={},
                    gaze_coords=(px_x, px_y),
                )
                self._drag_active = True
                self._drag_start_time = time.monotonic()
            elif action_type == "drag_end":
                cmd = Command(
                    text="gaze dwell drag end",
                    action="MOUSEUP",
                    source="gaze_dwell",
                    params={},
                    gaze_coords=(px_x, px_y),
                )
                self._drag_active = False
                self._drag_start_time = None

            await self._emit(cmd)
            return

        # --- Edge scroll and gaze-to-cursor (run every tick, non-exclusive) ---
        # These features use the continuous gaze stream and do NOT short-circuit
        # the priority rules below. They run after dwell routing (priority 3)
        # but before tilt/head (priorities 6-7).
        latest_gaze = self._gaze_buf.latest()
        gaze_is_recent = (
            latest_gaze is not None
            and (tick_start - latest_gaze.ts) < 0.3  # gaze data within 300ms
        )

        # Edge scroll: emit SCROLL commands when gaze is in edge zones
        if gaze_is_recent and self._feature_toggles.get("edge_scroll", True):
            # Respect tick budget: skip edge scroll if tick already exceeds 16ms
            tick_elapsed_ms = (time.monotonic() - tick_start) * 1000.0
            if tick_elapsed_ms < 16.0:
                scroll_cmds = self._check_edge_scroll(latest_gaze.x, latest_gaze.y)
                for scroll_cmd in scroll_cmds:
                    await self._emit(scroll_cmd)
            else:
                log.warning(
                    "Tick budget exceeded (%.1fms) — skipping edge scroll", tick_elapsed_ms
                )
        elif not gaze_is_recent:
            # No recent gaze data — reset edge scroll state so it doesn't persist
            if self._edge_scroll_active:
                self._edge_scroll_active = False
                self._edge_scroll_start = None
                self._edge_scroll_direction = None

        # Gaze-to-cursor: move cursor to smoothed gaze position
        if self._feature_toggles.get("gaze_cursor_mode", False):
            if gaze_is_recent:
                await self._apply_gaze_cursor(
                    latest_gaze.x, latest_gaze.y, self._gaze_buf._last_conf
                )
            else:
                # Gaze lost — _apply_gaze_cursor handles hold behavior via low confidence
                # Pass 0.0 confidence to trigger the hold-at-last-position logic
                if latest_gaze is not None:
                    await self._apply_gaze_cursor(latest_gaze.x, latest_gaze.y, 0.0)

        # Gaze stability — used by Rules 4 & 5
        stable = self._gaze_buf.stable_centroid(self._diag)

        # Rule 4 — Voice keyword "click":
        # In gaze-to-cursor mode, click at the EMA cursor position immediately —
        # no stability requirement because the cursor is already smoothed.
        # Outside gaze-to-cursor mode, require stable gaze as before.
        # Either way, consume the keyword so it never falls through to DICTATE.
        if self._voice_local and self._voice_local.lower().strip() == "click":
            self._voice_local = None
            if self._feature_toggles.get("gaze_cursor_mode", False) and self._gaze_cursor_ema is not None:
                ema_x, ema_y = self._gaze_cursor_ema
                px_x = max(0, min(self._w, round(ema_x * self._w)))
                px_y = max(0, min(self._h, round(ema_y * self._h)))
                cmd = Command(
                    text="gaze voice click",
                    action="CLICK",
                    source="multimodal",
                    gaze_coords=(px_x, px_y),
                )
                await self._emit(cmd)
            elif stable:
                cmd = Command(
                    text="gaze voice click",
                    action="CLICK",
                    source="multimodal",
                    gaze_coords=(int(stable[0] * self._w), int(stable[1] * self._h)),
                )
                await self._emit(cmd)
            # No EMA and no stable gaze — drop silently (don't fall through to DICTATE)
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
        # Position-mapped tilt_position takes priority over legacy velocity tilt.
        # In gaze-to-cursor mode, suppressed while gaze is active (prevents fighting).
        # When gaze is lost (not recent), tilt breaks through as an escape hatch —
        # clears EMA so gaze reinitialises fresh when it is reacquired.

        # If both tilt_position and legacy tilt are present, use tilt_position and discard legacy
        if self._tilt_position and self._tilt:
            self._tilt = None

        # Rule 6a — Absolute tilt position (position-mapped mode from iPad)
        if self._tilt_position:
            gaze_cursor_on = self._feature_toggles.get("gaze_cursor_mode", False)
            if gaze_cursor_on and gaze_is_recent:
                self._tilt_position = None  # gaze is driving — discard tilt position
            else:
                x, y = self._tilt_position
                self._tilt_position = None

                # Clear gaze EMA when tilt takes over (escape hatch)
                if gaze_cursor_on:
                    self._gaze_cursor_ema = None

                # EMA smoothing
                if not self._tilt_pos_initialized:
                    self._tilt_pos_ema_x = x
                    self._tilt_pos_ema_y = y
                    self._tilt_pos_initialized = True
                else:
                    a = self._cfg.tilt_pos_alpha
                    self._tilt_pos_ema_x = a * x + (1 - a) * self._tilt_pos_ema_x
                    self._tilt_pos_ema_y = a * y + (1 - a) * self._tilt_pos_ema_y

                # Convert to pixels
                px_x = round(self._tilt_pos_ema_x * self._w)
                px_y = round(self._tilt_pos_ema_y * self._h)

                # Clamp to screen bounds
                px_x = max(0, min(self._w - 1, px_x))
                px_y = max(0, min(self._h - 1, px_y))

                await asyncio.to_thread(pyautogui.moveTo, px_x, px_y, duration=0)
                return

        # Rule 6b — Legacy tilt navigation (velocity-based, no Command, no LLM)
        if self._tilt:
            gaze_cursor_on = self._feature_toggles.get("gaze_cursor_mode", False)
            if gaze_cursor_on and gaze_is_recent:
                self._tilt = None  # gaze is driving — discard tilt, no return
            else:
                rx, ry = self._tilt
                self._tilt = None
                dz = self._cfg.tilt_dead_zone

                # Apply dead zone — zero out values below threshold
                rx = rx if abs(rx) > dz else 0.0
                ry = ry if abs(ry) > dz else 0.0

                # EMA smoothing (α=0.3 balances responsiveness vs jitter)
                alpha = 0.3
                self._tilt_ema_x = alpha * rx + (1 - alpha) * self._tilt_ema_x
                self._tilt_ema_y = alpha * ry + (1 - alpha) * self._tilt_ema_y

                # rx = rotationRate.x: rotation around X-axis → vertical cursor
                #   positive rx (tilt top away) → cursor moves up (negative dy)
                # ry = rotationRate.y: rotation around Y-axis → horizontal cursor
                #   tilt right (negative ry) → cursor moves right (positive dx)
                self._tilt_accum_x += -self._tilt_ema_y * self._cfg.tilt_sensitivity
                self._tilt_accum_y += -self._tilt_ema_x * self._cfg.tilt_sensitivity

                # Only move when we've accumulated at least 1 pixel
                dx = int(self._tilt_accum_x)
                dy = int(self._tilt_accum_y)

                if dx or dy:
                    self._tilt_accum_x -= dx
                    self._tilt_accum_y -= dy
                    if gaze_cursor_on:
                        self._gaze_cursor_ema = None
                    await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
                return

        # Rule 7 — Head tracking (direct to pyautogui, no Command, no LLM)
        # Same escape-hatch logic as Rule 6.
        if self._head:
            gaze_cursor_on = self._feature_toggles.get("gaze_cursor_mode", False)
            if gaze_cursor_on and gaze_is_recent:
                self._head = None  # gaze is driving — discard head, no return
            else:
                pitch, yaw = self._head
                self._head = None
                dx = int(yaw * self._cfg.head_sensitivity)
                dy = int(-pitch * self._cfg.head_sensitivity)
                if dx or dy:
                    if gaze_cursor_on:
                        self._gaze_cursor_ema = None
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
    # Edge scroll detection
    # ---------------------------------------------------------------------- #

    def _check_edge_scroll(self, gaze_x: float, gaze_y: float) -> list[Command]:
        """Check if gaze is in an edge zone and manage scroll activation.

        Called every tick when gaze data is available. Returns a list of SCROLL
        Commands if edge scrolling is active and the activation delay has elapsed.
        Returns an empty list otherwise.

        Corner regions (where two edge zones overlap) produce two Commands — one
        for each direction — enabling diagonal scrolling.

        Args:
            gaze_x: Normalized gaze x coordinate [0.0, 1.0]
            gaze_y: Normalized gaze y coordinate [0.0, 1.0]

        Returns:
            A list of SCROLL Commands (0, 1, or 2 items).
        """
        # Skip edge scroll when feature is disabled (other scroll sources still work)
        if not self._feature_toggles["edge_scroll"]:
            return []

        zone_pct = self._cfg.edge_scroll_zone_pct

        # Determine which edge zones the gaze occupies (independently for each axis)
        directions: list[str] = []
        if gaze_y < zone_pct:
            directions.append("up")
        elif gaze_y > (1.0 - zone_pct):
            directions.append("down")

        if gaze_x < zone_pct:
            directions.append("left")
        elif gaze_x > (1.0 - zone_pct):
            directions.append("right")

        now = time.monotonic()

        # Gaze is NOT in any edge zone — stop immediately
        if not directions:
            self._edge_scroll_active = False
            self._edge_scroll_start = None
            self._edge_scroll_direction = None
            return []

        # Use a canonical key for the current direction set to detect changes
        direction_key = "+".join(sorted(directions))

        # Gaze entered a new zone or changed direction
        if direction_key != self._edge_scroll_direction:
            self._edge_scroll_active = False
            self._edge_scroll_start = now
            self._edge_scroll_direction = direction_key

        # Gaze is in a zone — check if activation delay has elapsed
        if self._edge_scroll_start is None:
            self._edge_scroll_start = now

        delay_s = self._cfg.edge_scroll_delay_ms / 1000.0
        elapsed = now - self._edge_scroll_start

        if elapsed < delay_s:
            # Still waiting for activation delay
            return []

        # Delay elapsed — scrolling is active
        self._edge_scroll_active = True

        # Build a Command for each active direction
        commands: list[Command] = []
        for direction in directions:
            # Compute scroll speed based on depth into zone
            if direction == "up":
                depth = (zone_pct - gaze_y) / zone_pct
            elif direction == "down":
                depth = (gaze_y - (1.0 - zone_pct)) / zone_pct
            elif direction == "left":
                depth = (zone_pct - gaze_x) / zone_pct
            else:  # right
                depth = (gaze_x - (1.0 - zone_pct)) / zone_pct

            # Clamp depth to [0.0, 1.0]
            depth = max(0.0, min(1.0, depth))

            # Linear interpolation: min_speed at inner boundary, max_speed at screen edge
            speed = self._cfg.edge_scroll_min_speed + depth * (
                self._cfg.edge_scroll_max_speed - self._cfg.edge_scroll_min_speed
            )
            clicks = max(1, round(speed))

            commands.append(Command(
                text=f"edge scroll {direction}",
                action="SCROLL",
                source="edge_scroll",
                params={"direction": direction, "clicks": clicks},
            ))

        return commands

    # ---------------------------------------------------------------------- #
    # Gaze-to-cursor
    # ---------------------------------------------------------------------- #

    async def _apply_gaze_cursor(self, gaze_x: float, gaze_y: float, conf: float) -> None:
        """Move cursor to smoothed gaze position when gaze-to-cursor mode is active.

        Applies EMA smoothing to raw gaze coordinates, clamps frame-to-frame
        displacement to max 5% of screen diagonal, and holds cursor at last
        position when confidence is below threshold or gaze is lost.

        Args:
            gaze_x: Normalized gaze x coordinate [0.0, 1.0]
            gaze_y: Normalized gaze y coordinate [0.0, 1.0]
            conf: Gaze tracking confidence [0.0, 1.0]
        """
        import pyautogui

        now = time.monotonic()

        # --- Low confidence: hold at last position ---
        if conf < self._cfg.gaze_cursor_conf_min:
            # Check if gaze has been lost too long
            if (self._gaze_cursor_last_seen > 0.0
                    and now - self._gaze_cursor_last_seen > self._cfg.gaze_cursor_lost_timeout_s):
                log.warning(
                    "Gaze-to-cursor: gaze lost for %.1fs, holding cursor at last position",
                    now - self._gaze_cursor_last_seen,
                )
            return

        # --- Valid gaze: update last-seen timestamp ---
        self._gaze_cursor_last_seen = now

        # --- EMA smoothing ---
        alpha = self._cfg.gaze_cursor_ema_alpha
        if self._gaze_cursor_ema is None:
            # First valid sample — initialize EMA directly
            ema_x, ema_y = gaze_x, gaze_y
        else:
            prev_ema_x, prev_ema_y = self._gaze_cursor_ema
            ema_x = alpha * gaze_x + (1.0 - alpha) * prev_ema_x
            ema_y = alpha * gaze_y + (1.0 - alpha) * prev_ema_y

        # --- Clamp displacement to max_jump_pct of screen diagonal ---
        if self._gaze_cursor_ema is not None:
            prev_ema_x, prev_ema_y = self._gaze_cursor_ema
            # Displacement in pixel space
            dx = ema_x - prev_ema_x
            dy = ema_y - prev_ema_y
            displacement_px = math.hypot(dx * self._w, dy * self._h)
            max_displacement_px = self._cfg.gaze_cursor_max_jump_pct * self._diag

            if displacement_px > max_displacement_px and displacement_px > 0:
                scale = max_displacement_px / displacement_px
                ema_x = prev_ema_x + dx * scale
                ema_y = prev_ema_y + dy * scale

        # --- Update EMA state ---
        self._gaze_cursor_ema = (ema_x, ema_y)

        # --- Convert to pixel coordinates (clamped to screen bounds) ---
        px_x = max(0, min(self._w, round(ema_x * self._w)))
        px_y = max(0, min(self._h, round(ema_y * self._h)))

        # --- Move cursor ---
        # moveTo is a single Win32 SendInput call (~microseconds); calling it
        # synchronously avoids thread-pool overhead at 60 Hz.
        self._gaze_cursor_last = (px_x, px_y)
        try:
            pyautogui.moveTo(px_x, px_y, _pause=False)
        except Exception as exc:
            log.error("Gaze-to-cursor: pyautogui.moveTo failed: %s", exc)

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
