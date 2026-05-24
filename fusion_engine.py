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
from gyro_bias_calibrator import GyroBiasCalibrator
from one_euro_filter import OneEuroFilter

if TYPE_CHECKING:
    from hybrid_coordinator import HybridCoordinator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal processing utility functions
# ---------------------------------------------------------------------------

def dead_zone_ramp(magnitude: float, inner: float, outer: float) -> float:
    """Smooth ramp from 0 at inner threshold to full magnitude at outer threshold.

    Uses cubic Hermite interpolation (smoothstep) for C1 continuity:
    zero first-derivative at both boundaries.

    Args:
        magnitude: Absolute input value (must be >= 0).
        inner: Inner threshold — below this, output is 0.
        outer: Outer threshold — above this, output equals magnitude.

    Returns:
        Ramped output value in [0, magnitude].
    """
    if magnitude <= inner:
        return 0.0
    if magnitude >= outer:
        return magnitude
    # Normalized position within ramp [0, 1]
    t = (magnitude - inner) / (outer - inner)
    # Smoothstep: 3t² - 2t³ (zero derivative at t=0 and t=1)
    s = t * t * (3.0 - 2.0 * t)
    return s * magnitude


def power_curve(value: float, exponent: float, sensitivity: float = 1.0) -> float:
    """Non-linear transfer function with sign preservation.

    output = sign(value) * |value|^exponent * sensitivity

    Args:
        value: Input value (can be negative).
        exponent: Power exponent (>= 1.0 for super-linear at extremes).
        sensitivity: Multiplier applied after the power function.

    Returns:
        Transformed value with preserved sign.
    """
    if value == 0.0:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return sign * (abs(value) ** exponent) * sensitivity


def head_acceleration_curve(
    velocity_deg_s: float,
    exponent: float = 1.8,
    tremor_threshold: float = 1.0,
    tick_hz: float = 60.0,
) -> float:
    """Map head angular velocity to cursor pixels/tick using a 3-zone model.

    Zone 1: |v| < tremor_threshold → 0 (tremor suppression)
    Zone 2-3: sign(v) * (|v| - tremor_threshold)^exponent / tick_hz

    Subtracting tremor_threshold ensures continuity at the zone 1→2 boundary
    (output starts at 0 when crossing the threshold).

    Args:
        velocity_deg_s: Head angular velocity in degrees/second.
        exponent: Power curve exponent (default 1.8).
        tremor_threshold: Below this velocity, output is zero (default 1.0 °/s).
        tick_hz: Tick rate for per-tick displacement (default 60.0).

    Returns:
        Cursor displacement in pixels/tick (before sensitivity multiplier).
    """
    if abs(velocity_deg_s) < tremor_threshold:
        return 0.0
    sign = 1.0 if velocity_deg_s > 0 else -1.0
    effective = abs(velocity_deg_s) - tremor_threshold
    return sign * (effective ** exponent) / tick_hz


class HeadStationaryLock:
    """Hysteresis-based cursor lock for head tracking.

    Locks cursor when head is still (below lock_threshold for lock_delay seconds).
    Unlocks when head moves above unlock_threshold.
    The hysteresis gap prevents rapid lock/unlock cycling at the boundary.
    """

    def __init__(
        self,
        lock_threshold: float = 0.5,
        unlock_threshold: float = 1.5,
        lock_delay: float = 0.2,
    ) -> None:
        self.lock_threshold = lock_threshold
        self.unlock_threshold = unlock_threshold
        self.lock_delay = lock_delay
        self.locked = False
        self._still_since: float | None = None

    def reset(self) -> None:
        """Reset lock state."""
        self.locked = False
        self._still_since = None

    def update(self, velocity_deg_s: float, now: float) -> bool:
        """Update lock state. Returns True if cursor should be locked.

        Args:
            velocity_deg_s: Magnitude of head angular velocity (°/s).
            now: Monotonic timestamp.

        Returns:
            True if cursor should be locked (no movement output).
        """
        if self.locked:
            if abs(velocity_deg_s) > self.unlock_threshold:
                self.locked = False
                self._still_since = None
            return self.locked

        if abs(velocity_deg_s) < self.lock_threshold:
            if self._still_since is None:
                self._still_since = now
            elif now - self._still_since >= self.lock_delay:
                self.locked = True
        else:
            self._still_since = None

        return self.locked


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

    # 1-Euro filter — tilt velocity mode
    tilt_vel_min_cutoff: float = 1.0         # Hz — minimum cutoff (jitter reduction at rest)
    tilt_vel_beta: float = 0.007             # speed coefficient (responsiveness to fast moves)
    tilt_vel_d_cutoff: float = 1.0           # Hz — derivative cutoff

    # 1-Euro filter — tilt position mode
    tilt_pos_min_cutoff: float = 0.5         # Hz — lower cutoff for position stability
    tilt_pos_beta: float = 0.004             # lower beta for smoother position tracking
    tilt_pos_d_cutoff: float = 1.0           # Hz — derivative cutoff

    # Power curve exponents
    tilt_vel_exponent: float = 2.0           # [1.0, 4.0] — quadratic for velocity
    tilt_pos_exponent: float = 1.5           # [1.0, 3.0] — sub-linear center, super-linear edges

    # Dead zone ramp (tilt velocity)
    dead_zone_inner: float = 0.05            # rad/s — below this: zero output
    dead_zone_ramp_mult: float = 1.5         # outer = inner + inner * mult → 0.125 rad/s

    # Head acceleration curve
    head_accel_exponent: float = 1.8         # [1.0, 3.0] — power curve for head velocity
    head_tremor_threshold: float = 1.0       # °/s — below this: zero output (tremor suppression)
    head_lock_threshold: float = 0.5         # °/s — lock cursor when below this for lock_delay
    head_unlock_threshold: float = 1.5       # °/s — unlock cursor when above this
    head_lock_delay: float = 0.2             # seconds of stillness before locking

    # Gaze fixation/confidence (PC-side thresholds)
    gaze_fixation_enter: float = 5.0         # °/s — enter fixation slowdown below this
    gaze_fixation_exit: float = 20.0         # °/s — exit fixation above this
    gaze_fixation_duration: float = 0.1      # seconds below threshold to activate
    gaze_fixation_slowdown: float = 0.5      # multiplier during fixation (50% speed)
    gaze_conf_freeze_threshold: float = 0.3  # freeze cursor below this confidence
    gaze_conf_freeze_duration: float = 0.5   # seconds below threshold to freeze
    gaze_conf_resume_threshold: float = 0.6  # resume above this confidence
    gaze_conf_resume_duration: float = 0.2   # seconds above threshold to resume

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
        # Pain-day effective config — starts as alias of base config; replaced by
        # apply_pain_day(True) without mutating _cfg so base can always be restored.
        self._effective_cfg: FusionConfig = self._cfg
        self._pain_day_active: bool = False

        # --- Input slots (cleared after consumption) ---
        self._touch: Optional[Command] = None
        self._sound: Optional[Command] = None
        self._gaze_dwell: Optional[tuple[float, float]] = None   # norm (x, y)
        self._gaze_dwell_action_type: str = "left_click"         # action type for pending dwell
        self._gaze_delta: Optional[tuple[float, float]] = None   # (dx, dy) pixels
        self._gaze_delta_conf: float = 1.0                       # tracking confidence
        self._gaze_delta_saccade: bool = False                   # saccade flag from iPad

        # --- Gaze fixation/confidence state (PC-side) ---
        self._gaze_fixation_active: bool = False
        self._gaze_fixation_start: float | None = None           # when velocity first dropped below threshold
        self._gaze_fixation_exit_start: float | None = None      # when velocity first rose above exit threshold
        self._gaze_conf_frozen: bool = False
        self._gaze_conf_low_start: float | None = None           # when confidence first dropped below freeze threshold
        self._gaze_conf_high_start: float | None = None          # when confidence first rose above resume threshold
        self._gaze_prev_dx: float = 0.0                          # previous frame dx for velocity estimation
        self._gaze_prev_dy: float = 0.0                          # previous frame dy for velocity estimation
        self._gesture: Optional[Command] = None
        self._voice_local: Optional[str] = None                  # keyword text
        self._voice: Optional[Command] = None
        self._tilt: Optional[tuple[float, float]] = None          # (rx, ry) rad/s
        # D2: last-known sensor values for throttled DB sampling (~1 Hz)
        self._last_tilt_sample: Optional[tuple[float, float]] = None
        self._last_gaze_delta_sample: Optional[tuple[float, float, float]] = None  # dx, dy, conf
        self._last_head_sample: Optional[tuple[float, float]] = None
        self._tick_count: int = 0   # moved here from run() so D2 sampling works
        self._db = None             # set via set_agent_db()
        self._tilt_filter_x: OneEuroFilter = OneEuroFilter(       # 1-Euro filter for tilt velocity X
            min_cutoff=self._cfg.tilt_vel_min_cutoff,
            beta=self._cfg.tilt_vel_beta,
            d_cutoff=self._cfg.tilt_vel_d_cutoff,
        )
        self._tilt_filter_y: OneEuroFilter = OneEuroFilter(       # 1-Euro filter for tilt velocity Y
            min_cutoff=self._cfg.tilt_vel_min_cutoff,
            beta=self._cfg.tilt_vel_beta,
            d_cutoff=self._cfg.tilt_vel_d_cutoff,
        )
        self._tilt_bias_cal: GyroBiasCalibrator = GyroBiasCalibrator()  # gyro bias calibration
        self._tilt_accum_x: float = 0.0                           # sub-pixel accumulator X
        self._tilt_accum_y: float = 0.0                           # sub-pixel accumulator Y
        self._head: Optional[tuple[float, float]] = None          # (pitch, yaw) degrees

        # --- Head stationary lock (hysteresis-based cursor freeze) ---
        self._head_lock = HeadStationaryLock(
            lock_threshold=self._cfg.head_lock_threshold,
            unlock_threshold=self._cfg.head_unlock_threshold,
            lock_delay=self._cfg.head_lock_delay,
        )

        # --- Tilt position (absolute positioning from iPad position-mapped mode) ---
        self._tilt_position: Optional[tuple[float, float]] = None  # (x, y) normalized [0,1]
        self._tilt_pos_filter_x: OneEuroFilter = OneEuroFilter(    # 1-Euro filter for position X
            min_cutoff=self._cfg.tilt_pos_min_cutoff,
            beta=self._cfg.tilt_pos_beta,
            d_cutoff=self._cfg.tilt_pos_d_cutoff,
        )
        self._tilt_pos_filter_y: OneEuroFilter = OneEuroFilter(    # 1-Euro filter for position Y
            min_cutoff=self._cfg.tilt_pos_min_cutoff,
            beta=self._cfg.tilt_pos_beta,
            d_cutoff=self._cfg.tilt_pos_d_cutoff,
        )

        # --- Ratchet state (tilt position re-centering) ---
        self._ratchet_active: bool = False
        self._ratchet_held_pos: Optional[tuple[int, int]] = None   # pixel position to hold

        # --- Sensor switch hold (mutual exclusion) ---
        self._switch_hold_until: float = 0.0                       # monotonic time until which cursor data is discarded

        # --- Cursor pause state ---
        self._cursor_paused: bool = False
        self._cursor_pause_time: float | None = None
        self._cursor_pause_last_toggle: float = 0.0
        self._cursor_pause_auto_resume_s: float = 60.0
        self._cursor_pause_debounce_s: float = 0.5

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

        # --- Wired components ---
        self._coordinator: Optional["HybridCoordinator"] = None
        self._gaze_calibrator = None   # GazeCalibrator — wired by main.py
        self._running = False
        # Tracked set prevents fire-and-forget tasks from being GC'd before completion
        # and surfaces unhandled exceptions via the done-callback log.
        self._route_tasks: set = set()

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_coordinator(self, coordinator: "HybridCoordinator") -> None:
        self._coordinator = coordinator

    def set_gaze_calibrator(self, calibrator) -> None:
        self._gaze_calibrator = calibrator
        log.info("FusionEngine: GazeCalibrator wired (calibrated=%s)", calibrator.is_calibrated)

    def set_agent_db(self, db) -> None:
        """Wire AgentDB for throttled sensor-stream persistence (D2, ~1 Hz)."""
        self._db = db

    async def load_rom_calibration(self, db) -> None:
        """D4: Load sensor range-of-motion from onboarding assessment.

        Applies the user's comfortable tilt range as the dead-zone inner
        threshold when no learned calibration has overridden the default.
        Only runs if the current config value is still at its factory default
        so it never clobbers a value already adapted by the trainer.
        """
        try:
            rom = await db.get_sensor_rom("tilt")
            default_cfg = FusionConfig()
            if "neutral" in rom:
                comfortable = rom["neutral"].get("comfortable_value")
                if comfortable and abs(
                    self._cfg.dead_zone_inner - default_cfg.dead_zone_inner
                ) < 0.001:
                    from dataclasses import replace as _dc_replace
                    self._cfg = _dc_replace(
                        self._cfg, dead_zone_inner=float(comfortable)
                    )
                    log.info(
                        "FusionEngine: ROM tilt dead_zone_inner set to %.3f from onboarding",
                        comfortable,
                    )
        except Exception as exc:
            log.debug("FusionEngine.load_rom_calibration failed (non-fatal): %s", exc)

    # ---------------------------------------------------------------------- #
    # Pain-day threshold adaptation
    # ---------------------------------------------------------------------- #

    def apply_pain_day(self, active: bool) -> None:
        """Apply or remove pain-day sensor threshold relaxations.

        Called by HybridCoordinator.route() once per command after reading
        TwinSnapshot.pain_day_active.  Uses dataclasses.replace to derive
        an adjusted FusionConfig without mutating self._cfg so the base
        can always be restored.

        Pain-day adjustments (all relax thresholds for tremor/fatigue):
            head_tremor_threshold : 1.0 → 2.0 °/s   (ignores more micro-tremor)
            head_lock_threshold   : 0.5 → 0.8 °/s   (locks cursor sooner)
            gaze_stability_pct    : 0.04 → 0.07      (accepts less stable gaze)
            dead_zone_inner       : 0.05 → 0.08 rad/s (larger tilt dead zone)
            sound_cooldown_s      : 0.5 → 0.3 s      (faster mouth-sound retry)
            gaze_conf_min         : 0.55 → 0.45       (accepts lower conf gaze)
            gaze_cursor_conf_min  : 0.55 → 0.45
        """
        if active == self._pain_day_active:
            return  # idempotent — no config object created on every tick

        self._pain_day_active = active

        if not active:
            self._effective_cfg = self._cfg
            # Restore HeadStationaryLock thresholds
            self._head_lock.lock_threshold   = self._cfg.head_lock_threshold
            self._head_lock.unlock_threshold = self._cfg.head_unlock_threshold
            log.info("FusionEngine: pain-day thresholds restored to baseline")
            return

        from dataclasses import replace as _dc_replace
        self._effective_cfg = _dc_replace(
            self._cfg,
            head_tremor_threshold=2.0,
            head_lock_threshold=0.8,
            head_unlock_threshold=2.5,  # wider hysteresis on pain day to suppress tremor
            gaze_stability_pct=0.07,
            dead_zone_inner=0.08,
            sound_cooldown_s=0.3,
            gaze_conf_min=0.45,
            gaze_cursor_conf_min=0.45,
        )
        # HeadStationaryLock reads thresholds directly — sync immediately
        self._head_lock.lock_threshold   = self._effective_cfg.head_lock_threshold
        self._head_lock.unlock_threshold = self._effective_cfg.head_unlock_threshold
        log.info("FusionEngine: pain-day thresholds applied")

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
        if now - self._last_sound_ts < self._effective_cfg.sound_cooldown_s:
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

    def on_gaze_delta(self, dx: float, dy: float, conf: float = 1.0, saccade: bool = False) -> None:
        """Receive relative gaze movement deltas from iPad.

        Moves the cursor by (dx, dy) pixels. This replaces absolute gaze
        positioning — works regardless of iPad angle relative to monitors.

        Args:
            dx: Horizontal cursor displacement (pixels).
            dy: Vertical cursor displacement (pixels).
            conf: Tracking confidence [0.0, 1.0]. Used for fixation slowdown.
            saccade: True if iPad detected a saccade (output already suppressed).
        """
        self._gaze_delta = (dx, dy)
        self._gaze_delta_conf = conf
        self._gaze_delta_saccade = saccade
        self._last_gaze_delta_sample = (dx, dy, conf)  # D2: cache for DB sampling

    def on_gaze_dwell(
        self,
        x: float,
        y: float,
        action_type: str = "left_click",
        ray_dir: Optional[tuple[float, float, float]] = None,
    ) -> None:
        # If a calibrated gaze calibrator is wired and a fresh ray arrived,
        # override the normalised (x, y) with the calibrator's absolute projection.
        if ray_dir is not None and self._gaze_calibrator is not None:
            result = self._gaze_calibrator.project(ray_dir)
            if result is not None:
                x = result[0] / self._w
                y = result[1] / self._h
        self._gaze_dwell = (x, y)
        self._gaze_dwell_action_type = action_type

    def on_tilt(self, rx: float, ry: float) -> None:
        self._tilt = (rx, ry)
        self._last_tilt_sample = (rx, ry)  # D2: cache for DB sampling

    def on_tilt_position(self, x: float, y: float) -> None:
        """Receive absolute position from iPad tilt sensor (position-mapped mode)."""
        self._tilt_position = (x, y)

    def on_tilt_ratchet(self) -> None:
        """Handle ratchet trigger from iPad — hold cursor at current position.

        The cursor stays frozen until the next tilt input exceeds the dead zone
        from the new neutral point. The iPad has already captured the new neutral
        gravity vector; this just tells the PC to hold position.
        """
        import pyautogui
        # Record current cursor position as the held position
        pos = pyautogui.position()
        self._ratchet_held_pos = (pos.x, pos.y)
        self._ratchet_active = True
        # Reset tilt position filter so it starts fresh from the new neutral
        self._tilt_pos_filter_x.reset()
        self._tilt_pos_filter_y.reset()
        log.info("Ratchet activated — holding cursor at (%d, %d)", pos.x, pos.y)

    def on_sensor_switch(self, from_sensor: str | None, to_sensor: str) -> None:
        """Handle cursor sensor switch — hold cursor for 200ms to prevent jump.

        Called when iPad SensorManager switches between tilt/gaze/head.
        Discards all cursor-sensor data during the hold window.
        """
        self._switch_hold_until = time.monotonic() + 0.2
        # Reset filter state for the new sensor
        if to_sensor == "tilt":
            self._tilt_pos_filter_x.reset()
            self._tilt_pos_filter_y.reset()
            self._tilt_filter_x.reset()
            self._tilt_filter_y.reset()
            self._tilt_bias_cal.reset()
            self._tilt_accum_x = 0.0
            self._tilt_accum_y = 0.0
        elif to_sensor == "head":
            self._head_lock.reset()
        # Gaze filters are on iPad side — no PC state to reset
        log.info("Sensor switch: %s → %s (200ms hold)", from_sensor, to_sensor)

    def on_cursor_pause(self) -> None:
        """Pause all cursor sensors — cursor stays at current position.

        Debounced: ignores triggers within 500ms of last toggle.
        """
        now = time.monotonic()
        if now - self._cursor_pause_last_toggle < self._cursor_pause_debounce_s:
            return
        self._cursor_pause_last_toggle = now

        if not self._cursor_paused:
            self._cursor_paused = True
            self._cursor_pause_time = now
            # Reset accumulators so no drift builds up
            self._tilt_accum_x = 0.0
            self._tilt_accum_y = 0.0
            log.info("Cursor paused")

    def on_cursor_resume(self) -> None:
        """Resume cursor sensors — re-zero from current physical state.

        Debounced: ignores triggers within 500ms of last toggle.
        """
        now = time.monotonic()
        if now - self._cursor_pause_last_toggle < self._cursor_pause_debounce_s:
            return
        self._cursor_pause_last_toggle = now

        if self._cursor_paused:
            self._cursor_paused = False
            self._cursor_pause_time = None
            # Re-zero: reset all sensor filters so cursor doesn't jump
            self._tilt_pos_filter_x.reset()
            self._tilt_pos_filter_y.reset()
            self._tilt_filter_x.reset()
            self._tilt_filter_y.reset()
            self._tilt_accum_x = 0.0
            self._tilt_accum_y = 0.0
            self._head_lock.reset()
            self._gaze_fixation_active = False
            self._gaze_conf_frozen = False
            log.info("Cursor resumed (sensors re-zeroed)")

    def on_head(self, pitch: float, yaw: float) -> None:
        self._head = (pitch, yaw)
        self._last_head_sample = (pitch, yaw)  # D2: cache for DB sampling

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
        cfg = self._effective_cfg  # pain-day-adjusted config alias for this tick

        # Sync gaze buffer thresholds from effective config (changes on pain day)
        self._gaze_buf._stability_pct = cfg.gaze_stability_pct
        self._gaze_buf._conf_min      = cfg.gaze_conf_min

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

        # --- Cursor pause: auto-resume after timeout ---
        if self._cursor_paused and self._cursor_pause_time:
            if tick_start - self._cursor_pause_time > self._cursor_pause_auto_resume_s:
                log.warning("Cursor pause auto-resume after %.0fs", self._cursor_pause_auto_resume_s)
                self.on_cursor_resume()

        # --- Sensor switch hold: discard cursor data during 200ms window ---
        # Touch and sound still pass through (they bypass all gates).
        switch_hold_active = tick_start < self._switch_hold_until

        # Rule 1 — Touch (bypass all gates, even during pause/switch hold)
        if self._touch:
            cmd, self._touch = self._touch, None
            await self._emit(cmd)
            return

        # Rule 2 — Sound action (bypass all gates, even during pause)
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

        # Gaze-to-cursor: move cursor to smoothed gaze position.
        # Capture return value — if True, suppress gaze-delta below to prevent
        # double cursor movement (absolute moveTo then relative moveRel in same tick).
        # Gated by cursor_paused and switch_hold_active so gaze respects the same
        # pause semantics as tilt and head (previously it bypassed them).
        _gaze_cursor_moved = False
        if (self._feature_toggles.get("gaze_cursor_mode", False)
                and not self._cursor_paused and not switch_hold_active):
            if gaze_is_recent:
                _gaze_cursor_moved = await self._apply_gaze_cursor(
                    latest_gaze.x, latest_gaze.y, self._gaze_buf._last_conf
                )
            else:
                # Gaze lost — _apply_gaze_cursor handles hold behavior via low confidence
                if latest_gaze is not None:
                    _gaze_cursor_moved = await self._apply_gaze_cursor(
                        latest_gaze.x, latest_gaze.y, 0.0
                    )

        # Gaze delta: relative cursor movement from eye direction changes
        # Includes fixation slowdown and confidence freeze (Req 15).
        # BUG FIX: skip if gaze-to-cursor already fired a moveTo this tick —
        # prevents absolute + relative compound displacement in the same frame.
        if self._gaze_delta and not _gaze_cursor_moved and not self._cursor_paused and not switch_hold_active:
            dx, dy = self._gaze_delta
            conf = self._gaze_delta_conf
            self._gaze_delta = None

            now = time.monotonic()

            # --- Confidence freeze: freeze cursor when confidence is too low ---
            if not self._gaze_conf_frozen:
                if conf < cfg.gaze_conf_freeze_threshold:
                    if self._gaze_conf_low_start is None:
                        self._gaze_conf_low_start = now
                    elif now - self._gaze_conf_low_start >= cfg.gaze_conf_freeze_duration:
                        self._gaze_conf_frozen = True
                        self._gaze_conf_low_start = None
                        self._gaze_conf_high_start = None
                else:
                    self._gaze_conf_low_start = None
            else:
                # Frozen — check if confidence recovered enough to resume
                if conf >= cfg.gaze_conf_resume_threshold:
                    if self._gaze_conf_high_start is None:
                        self._gaze_conf_high_start = now
                    elif now - self._gaze_conf_high_start >= cfg.gaze_conf_resume_duration:
                        self._gaze_conf_frozen = False
                        self._gaze_conf_high_start = None
                        self._gaze_conf_low_start = None
                else:
                    self._gaze_conf_high_start = None

            # If frozen, discard gaze data entirely
            if self._gaze_conf_frozen:
                pass  # cursor stays at last position
            elif abs(dx) > 0.01 or abs(dy) > 0.01:
                # --- Fixation slowdown: reduce speed during fixation ---
                # Estimate gaze velocity from delta magnitude (already in pixels)
                # Convert to approximate °/s: pixels / sensitivity ≈ radians, * 180/π * 60fps
                gaze_vel_approx = math.hypot(dx, dy)  # pixel-space velocity proxy

                if not self._gaze_fixation_active:
                    if gaze_vel_approx < cfg.gaze_fixation_enter:
                        if self._gaze_fixation_start is None:
                            self._gaze_fixation_start = now
                        elif now - self._gaze_fixation_start >= cfg.gaze_fixation_duration:
                            self._gaze_fixation_active = True
                            self._gaze_fixation_start = None
                    else:
                        self._gaze_fixation_start = None
                else:
                    # Active fixation — check exit condition
                    if gaze_vel_approx > cfg.gaze_fixation_exit:
                        if self._gaze_fixation_exit_start is None:
                            self._gaze_fixation_exit_start = now
                        elif now - self._gaze_fixation_exit_start >= 0.03:  # 30ms sustained
                            self._gaze_fixation_active = False
                            self._gaze_fixation_exit_start = None
                    else:
                        self._gaze_fixation_exit_start = None

                # Apply fixation slowdown multiplier
                speed_mult = cfg.gaze_fixation_slowdown if self._gaze_fixation_active else 1.0
                final_dx = dx * speed_mult
                final_dy = dy * speed_mult

                if abs(final_dx) > 0.5 or abs(final_dy) > 0.5:
                    await asyncio.to_thread(pyautogui.moveRel, int(final_dx), int(final_dy), duration=0)

        # Gaze stability — used by Rules 4 & 5.
        # _gaze_buf thresholds already synced from _effective_cfg at tick start.
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
            else:
                # No gaze target available — inform user instead of silent drop.
                # Still return here (don't fall through to DICTATE with text="click").
                clarify_cmd = Command(
                    text="no gaze target — where would you like to click?",
                    action="CLARIFY",
                    source="multimodal",
                )
                await self._emit(clarify_cmd)
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

        # --- Pause / Switch hold guard for cursor-driving sensors ---
        # When paused or during sensor switch, discard all cursor sensor data
        # (tilt, head, gaze delta) without accumulating state.
        # Note: gaze_delta is consumed earlier and already gated above;
        # this block clears residual tilt/head state only.
        if self._cursor_paused or switch_hold_active:
            # Consume and discard cursor sensor inputs
            self._tilt_position = None
            self._tilt = None
            self._head = None
            self._gaze_delta = None  # belt-and-suspenders: already gated before this point
            # Reset accumulators to prevent drift buildup
            self._tilt_accum_x = 0.0
            self._tilt_accum_y = 0.0
            # Still allow voice/gesture rules below to fire
            # (they don't move the cursor directly)

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

                now = time.monotonic()

                # 1-Euro filter (per-axis adaptive smoothing)
                filtered_x = self._tilt_pos_filter_x(x, timestamp=now)
                filtered_y = self._tilt_pos_filter_y(y, timestamp=now)

                # Ratchet: hold cursor until displacement from new neutral exceeds dead zone.
                # When active, suppress tilt movement but fall through to Rules 7-10 so
                # voice and gesture commands still fire (previously an early return silenced them).
                if self._ratchet_active:
                    disp = math.hypot(filtered_x - 0.5, filtered_y - 0.5)
                    if disp < 0.04:  # ~2° at 25° range — still within dead zone
                        pass  # skip cursor movement; fall through to voice/gesture rules
                    else:
                        # Exceeded dead zone — deactivate and apply movement below
                        self._ratchet_active = False
                        self._ratchet_held_pos = None
                        log.info("Ratchet deactivated — tilt exceeded dead zone")

                if not self._ratchet_active:
                    # Power curve on displacement from center (per-axis)
                    dx = filtered_x - 0.5
                    dy = filtered_y - 0.5
                    exp = cfg.tilt_pos_exponent
                    # Normalize to [-1, 1], apply power curve, denormalize back to [0, 1]
                    curved_x = 0.5 + power_curve(dx / 0.5, exp) * 0.5 if dx != 0.0 else 0.5
                    curved_y = 0.5 + power_curve(dy / 0.5, exp) * 0.5 if dy != 0.0 else 0.5

                    # Convert to pixels
                    px_x = round(curved_x * self._w)
                    px_y = round(curved_y * self._h)

                    # Clamp to screen bounds
                    px_x = max(0, min(self._w - 1, px_x))
                    px_y = max(0, min(self._h - 1, px_y))

                    await asyncio.to_thread(pyautogui.moveTo, px_x, px_y, duration=0)
                    return
                # Ratchet hold: no cursor movement — fall through to Rules 7-10

        # Rule 6b — Legacy tilt navigation (velocity-based, no Command, no LLM)
        if self._tilt:
            gaze_cursor_on = self._feature_toggles.get("gaze_cursor_mode", False)
            if gaze_cursor_on and gaze_is_recent:
                self._tilt = None  # gaze is driving — discard tilt, no return
            else:
                rx, ry = self._tilt
                self._tilt = None

                now = time.monotonic()

                # 1. Feed raw values to bias calibrator (state machine update)
                self._tilt_bias_cal.update(rx, ry, now=now)

                # 2. Subtract estimated gyro bias
                bias_x, bias_y = self._tilt_bias_cal.get_current_bias(now=now)
                rx -= bias_x
                ry -= bias_y

                # 3. Check uncalibrated suppression — fall through to Rules 8–10 if suppressed
                #    (previously: early return blocked voice/gesture while iPad was being held)
                _tilt_suppressed = self._tilt_bias_cal.should_suppress(rx, ry)

                if not _tilt_suppressed:
                    # 4. 1-Euro adaptive filter (per-axis)
                    rx = self._tilt_filter_x(rx, timestamp=now)
                    ry = self._tilt_filter_y(ry, timestamp=now)

                    # 5. Dead zone ramp (smoothstep transition)
                    inner = self._effective_cfg.dead_zone_inner
                    outer = inner + inner * self._effective_cfg.dead_zone_ramp_mult
                    rx_mag = abs(rx)
                    ry_mag = abs(ry)
                    rx_ramped = dead_zone_ramp(rx_mag, inner, outer)
                    ry_ramped = dead_zone_ramp(ry_mag, inner, outer)
                    # Restore sign
                    rx_ramped = rx_ramped if rx >= 0 else -rx_ramped
                    ry_ramped = ry_ramped if ry >= 0 else -ry_ramped

                    # 6. Power curve transfer function
                    sensitivity = self._effective_cfg.tilt_sensitivity
                    exp = self._effective_cfg.tilt_vel_exponent
                    cursor_dx = power_curve(rx_ramped, exp, sensitivity)
                    cursor_dy = power_curve(ry_ramped, exp, sensitivity)

                    # rx = rotationRate.x: rotation around X-axis → vertical cursor
                    #   positive rx (tilt top away) → cursor moves up (negative dy)
                    # ry = rotationRate.y: rotation around Y-axis → horizontal cursor
                    #   tilt right (negative ry) → cursor moves right (positive dx)
                    self._tilt_accum_x += -cursor_dy  # ry → horizontal
                    self._tilt_accum_y += -cursor_dx  # rx → vertical

                    # Only move when we've accumulated at least 1 pixel.
                    # BUG FIX: return ONLY when pixels were produced — otherwise fall
                    # through to Rules 8–10 so voice/gesture fire while tilt is sub-dead-zone.
                    dx = int(self._tilt_accum_x)
                    dy = int(self._tilt_accum_y)

                    if dx or dy:
                        self._tilt_accum_x -= dx
                        self._tilt_accum_y -= dy
                        if gaze_cursor_on:
                            self._gaze_cursor_ema = None
                        await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
                        return  # cursor moved — this tick is done
                # Suppressed or sub-dead-zone: fall through to Rules 8–10

        # Rule 7 — Head tracking (direct to pyautogui, no Command, no LLM)
        # Uses 3-zone acceleration curve + stationary lock with hysteresis.
        # Same escape-hatch logic as Rule 6.
        if self._head:
            gaze_cursor_on = self._feature_toggles.get("gaze_cursor_mode", False)
            if gaze_cursor_on and gaze_is_recent:
                self._head = None  # gaze is driving — discard head, no return
            else:
                pitch, yaw = self._head
                self._head = None
                now = time.monotonic()

                # Compute velocity magnitude for stationary lock
                velocity_mag = math.hypot(pitch, yaw)

                # Stationary lock: freeze cursor when head is still
                if self._head_lock.update(velocity_mag, now):
                    return  # locked — no cursor movement

                # 3-zone acceleration curve (per-axis)
                # Tremor suppression + power curve with configurable exponent
                exp    = self._effective_cfg.head_accel_exponent
                tremor = self._effective_cfg.head_tremor_threshold  # pain-day aware
                tick_hz = self._effective_cfg.tick_hz

                accel_pitch = head_acceleration_curve(pitch, exp, tremor, tick_hz)
                accel_yaw = head_acceleration_curve(yaw, exp, tremor, tick_hz)

                # Apply sensitivity multiplier
                dx = int(accel_yaw * self._effective_cfg.head_sensitivity)
                dy = int(accel_pitch * self._effective_cfg.head_sensitivity)

                # BUG FIX: return ONLY when pixels were produced — otherwise fall
                # through to Rules 8–10 so voice/gesture fire while head is below
                # tremor threshold (e.g. user with low-level RA head tremor).
                if dx or dy:
                    if gaze_cursor_on:
                        self._gaze_cursor_ema = None
                    await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
                    return  # cursor moved — this tick is done
                # Sub-tremor or zero: fall through to Rules 8–10

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

        # D2: throttled sensor-stream persistence (~1 Hz = every 60 ticks).
        # Uses fire-and-forget asyncio.ensure_future so DB writes never stall
        # the 60 Hz tick loop. Caches are updated by on_tilt/on_gaze_delta/on_head.
        if self._db is not None and self._db.available and self._tick_count % 60 == 0:
            if self._last_tilt_sample is not None:
                rx, ry = self._last_tilt_sample
                asyncio.ensure_future(
                    self._db.insert_sensor_event("tilt", x=rx, y=ry)
                )
            if self._last_gaze_delta_sample is not None:
                dx, dy, conf = self._last_gaze_delta_sample
                asyncio.ensure_future(
                    self._db.insert_sensor_event(
                        "gaze_delta", x=dx, y=dy, confidence=conf
                    )
                )
            if self._last_head_sample is not None:
                pitch, yaw = self._last_head_sample
                asyncio.ensure_future(
                    self._db.insert_sensor_event("head", x=pitch, y=yaw)
                )

    async def _emit(self, cmd: Command) -> None:
        if self._coordinator:
            # Fire-and-forget: prevents 200-600ms LLM inference from blocking the 60Hz tick loop.
            task = asyncio.create_task(self._coordinator.route(cmd))
            self._route_tasks.add(task)
            task.add_done_callback(self._route_tasks.discard)
            task.add_done_callback(
                lambda t: log.error("FusionEngine route task failed: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )
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

    async def _apply_gaze_cursor(self, gaze_x: float, gaze_y: float, conf: float) -> bool:
        """Move cursor to smoothed gaze position when gaze-to-cursor mode is active.

        Applies EMA smoothing to raw gaze coordinates, clamps frame-to-frame
        displacement to max 5% of screen diagonal, and holds cursor at last
        position when confidence is below threshold or gaze is lost.

        Args:
            gaze_x: Normalized gaze x coordinate [0.0, 1.0]
            gaze_y: Normalized gaze y coordinate [0.0, 1.0]
            conf: Gaze tracking confidence [0.0, 1.0]

        Returns:
            True if cursor was actually moved (moveTo called), False if held.
            Used by _tick() to suppress gaze-delta in the same frame.
        """
        import pyautogui

        now = time.monotonic()

        # --- Low confidence: hold at last position ---
        if conf < self._effective_cfg.gaze_cursor_conf_min:
            # Check if gaze has been lost too long
            if (self._gaze_cursor_last_seen > 0.0
                    and now - self._gaze_cursor_last_seen > self._effective_cfg.gaze_cursor_lost_timeout_s):
                log.warning(
                    "Gaze-to-cursor: gaze lost for %.1fs, holding cursor at last position",
                    now - self._gaze_cursor_last_seen,
                )
            return False  # held, not moved

        # --- Valid gaze: update last-seen timestamp ---
        self._gaze_cursor_last_seen = now

        # --- EMA smoothing ---
        alpha = self._effective_cfg.gaze_cursor_ema_alpha
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
            max_displacement_px = self._effective_cfg.gaze_cursor_max_jump_pct * self._diag

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
            return True   # cursor moved — caller suppresses gaze-delta this tick
        except Exception as exc:
            log.error("Gaze-to-cursor: pyautogui.moveTo failed: %s", exc)
            return False  # movement failed — allow gaze-delta as fallback

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def run(self) -> None:
        self._running = True
        # _tick_count is initialised in __init__ (moved for D2 sensor sampling)
        interval = 1.0 / self._cfg.tick_hz
        log.info("FusionEngine running at %.0f Hz", self._cfg.tick_hz)
        _slow_tick_threshold = interval * 2  # warn if tick body takes > 2× the interval
        while self._running:
            t0 = time.monotonic()
            try:
                await self._tick()
                self._tick_count += 1
            except Exception as exc:
                log.error("FusionEngine tick error: %s", exc)
            elapsed = time.monotonic() - t0
            if elapsed > _slow_tick_threshold:
                log.warning("FusionEngine slow tick: %.1f ms (budget %.1f ms)",
                            elapsed * 1000, interval * 1000)
            await asyncio.sleep(max(0.0, interval - elapsed))

    def stop(self) -> None:
        self._running = False
        log.info("FusionEngine stopped")
