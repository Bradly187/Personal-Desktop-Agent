"""FusionEngine — 6-level priority sensor fusion at 60 Hz.

Receives sensor events from IPadBridge and on each tick emits at most one
Command to HybridCoordinator, or moves the cursor directly for tilt
events (which bypass the LLM entirely).

Priority order (highest → lowest):
  1  touch       — iPad CommandPad tap, bypass all gates
  2  voice click — Speech keyword "click" → click at current cursor position
  3  tilt        — Core Motion tilt → pyautogui.moveRel (no Command)
  4  gesture     — MediaPipe gesture command
  5  voice_local — Speech Framework keyword (skip Gate 1)
  6  voice       — PC Whisper transcription (full 4-gate)

Gaze and head-pose tracking were removed: the target iPad has no TrueDepth
sensor, so ARFaceTrackingConfiguration is unavailable and both produced no
data. Mouth-sound control (cluck/pop/hiss) was removed — the sounds fired
incidentally. See diagrams/06-fusion-routing.md for the decision flowchart.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from core.command_executor import Command
from calibration.gyro_bias_calibrator import GyroBiasCalibrator
from sensors.one_euro_filter import OneEuroFilter

if TYPE_CHECKING:
    from core.hybrid_coordinator import HybridCoordinator

log = logging.getLogger(__name__)

from core.async_utils import fire_and_log


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


def _detect_virtual_screen(fallback_w: int, fallback_h: int) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the full virtual desktop.

    The virtual desktop is the bounding rectangle spanning *all* monitors. A
    monitor positioned to the left of / above the primary has a negative origin,
    so `left`/`top` can be < 0 and `width`/`height` cover every screen. This is
    what lets absolute tilt positioning reach a side monitor — `pyautogui.size()`
    only ever reports the primary monitor.

    Windows: SM_*VIRTUALSCREEN system metrics. Other platforms (or on any
    failure) fall back to a single (0, 0, fallback_w, fallback_h) screen.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77,
            # SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
            left = int(user32.GetSystemMetrics(76))
            top = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return (left, top, width, height)
        except Exception as exc:  # pragma: no cover - platform/ctypes quirks
            log.debug("virtual-screen detection failed (%s) — using primary", exc)
    return (0, 0, int(fallback_w), int(fallback_h))


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


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class FusionConfig:
    tick_hz: float = 60.0
    tilt_dead_zone: float = 0.05           # rad/s below which tilt is ignored
    tilt_sensitivity: float = 200.0        # pixels moved per rad/s

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

    # Max per-frame cursor displacement (px) for tilt VELOCITY mode. The power
    # curve (exponent 2) amplifies fast input, so an involuntary tremor jerk can
    # produce a large single-frame fling. Clamping each axis caps that without
    # affecting normal control — at sensitivity 200 a brisk 0.7 rad/s move is
    # ~98 px/frame, well under the cap. Set <= 0 to disable.
    tilt_max_px_per_frame: float = 150.0


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
        # Virtual-desktop bounds (left, top, width, height) spanning ALL monitors.
        # Absolute tilt positioning maps into this rectangle so the cursor can
        # reach a side monitor. Initialised to the primary (deterministic for
        # construction/tests); refresh_screen_geometry() picks up the real
        # multi-monitor layout once the engine runs, and re-checks periodically
        # so a resolution change / hot-plugged monitor is handled without restart.
        self._vleft: int = 0
        self._vtop: int = 0
        self._vw: int = screen_width
        self._vh: int = screen_height
        self._cfg = config or FusionConfig()
        # Pain-day effective config — starts as alias of base config; replaced by
        # apply_pain_day(True) without mutating _cfg so base can always be restored.
        self._effective_cfg: FusionConfig = self._cfg
        self._pain_day_tilt: bool = False
        self._pain_day_active: bool = False

        # --- Input slots (cleared after consumption) ---
        self._touch: Optional[Command] = None
        self._gesture: Optional[Command] = None
        self._voice_local: Optional[str] = None                  # keyword text
        self._voice: Optional[Command] = None
        self._tilt: Optional[tuple[float, float]] = None          # (rx, ry) rad/s
        # D2: last-known sensor values for throttled DB sampling (~1 Hz)
        self._last_tilt_sample: Optional[tuple[float, float]] = None
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

        # --- Cursor position cache (updated at 10 Hz by background task) ---
        # Avoids blocking pyautogui.position() OS calls inside the 60 Hz tick loop.
        self._cursor_pos: tuple[int, int] = (screen_width // 2, screen_height // 2)
        self._cursor_cache_task: Optional[asyncio.Task] = None

        # --- Cursor gravity (Phase 3) — wired via set_target_cache() ---
        self._target_cache = None
        # Bias the tilt cursor toward a clickable within this radius (px).
        self._gravity_radius: int = 90
        # Max nudge applied at the target's edge → 0 at the rim, full at center.
        self._gravity_max_pull: int = 18

        # Gaze, gaze-to-cursor, edge-scroll, and drag state were removed with the
        # gaze pipeline (gaze dwell was the only drag initiator).

        # --- Feature toggles (synced from iPad via bridge) ---
        # All previous toggles (gaze dwell variants, edge_scroll, gaze_cursor_mode)
        # were gaze features and have been removed. The mechanism is retained
        # (empty) so the bridge set_feature_toggle path stays wired without
        # special-casing; unknown features are rejected by set_feature_toggle.
        self._feature_toggles: dict[str, bool] = {}

        # --- Wired components ---
        self._coordinator: Optional["HybridCoordinator"] = None
        self._metrics = None           # metrics.Metrics — wired by main.py
        self._session_id: int = -1     # set via set_session_id()
        self._last_active_source: Optional[str] = None   # tracks most recent source for telemetry
        self._last_gesture_conf: Optional[float] = None  # last gesture confidence for telemetry
        self._acoustic_profiler = None  # AcousticProfiler — wired by main.py for rms_ambient
        self._running = False
        # Tracked set prevents fire-and-forget tasks from being GC'd before completion
        # and surfaces unhandled exceptions via the done-callback log.
        self._route_tasks: set = set()
        self._scheduler = None  # AccessibilityScheduler — wired via set_scheduler()

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_coordinator(self, coordinator: "HybridCoordinator") -> None:
        self._coordinator = coordinator

    def set_scheduler(self, scheduler) -> None:
        """Wire AccessibilityScheduler so _emit() submits at the correct priority."""
        self._scheduler = scheduler

    @staticmethod
    def _source_to_priority(source: str):
        """Map a Command source string to its scheduler Priority tier."""
        from core.scheduler import Priority
        _MAP = {
            "touch":        Priority.ACCESSIBILITY,
            "multimodal":   Priority.ACCESSIBILITY,  # voice-click bypass
            "voice":        Priority.VOICE,
            "voice_local":  Priority.VOICE,
            "gesture":      Priority.GESTURE,
        }
        return _MAP.get(source, Priority.ACCESSIBILITY)

    def refresh_screen_geometry(self) -> bool:
        """Re-query the virtual desktop bounds; update them if they changed.

        Lets a resolution change or a newly plugged-in / removed monitor take
        effect at runtime (absolute tilt then maps across the new layout). Cheap
        GetSystemMetrics calls — safe to poll. Returns True if the bounds moved.
        Blocking (ctypes), so callers on the event loop should use to_thread.
        """
        new = _detect_virtual_screen(self._w, self._h)
        if new != (self._vleft, self._vtop, self._vw, self._vh):
            self._vleft, self._vtop, self._vw, self._vh = new
            log.info(
                "FusionEngine: screen geometry updated → %dx%d at (%d, %d) "
                "(spans %s)",
                self._vw, self._vh, self._vleft, self._vtop,
                "multiple monitors" if (self._vw, self._vh) != (self._w, self._h)
                or (self._vleft, self._vtop) != (0, 0) else "primary only",
            )
            return True
        return False

    def set_agent_db(self, db) -> None:
        """Wire AgentDB for throttled sensor-stream persistence (~1 Hz)."""
        self._db = db

    def set_metrics(self, metrics) -> None:
        """Wire Metrics singleton for real-time counter/gauge updates."""
        self._metrics = metrics

    def set_session_id(self, session_id: int) -> None:
        """Set current session ID for telemetry rows."""
        self._session_id = session_id

    def set_acoustic_profiler(self, profiler) -> None:
        """Wire AcousticProfiler so rms_ambient is included in telemetry."""
        self._acoustic_profiler = profiler

    def set_target_cache(self, cache) -> None:
        """Wire the ClickableTargetCache for cursor gravity (Phase 3).

        When set, tilt cursor positioning is gently biased toward a nearby
        clickable target so the cursor 'sticks' to buttons, making it easier to
        settle on them without fine motor control. No-op while the cache has no
        targets (unsupported app / UIA unavailable).
        """
        self._target_cache = cache

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

    def apply_pain_day(self, tilt: bool) -> None:
        """Apply or remove pain-day sensor threshold relaxations.

        Called by HybridCoordinator.route() once per command. The flare_profile
        ``tilt_degrades`` flag gates the tilt dead zone relaxation.

        Uses dataclasses.replace to derive an adjusted FusionConfig without
        mutating self._cfg so the base can always be restored.

        Pain-day adjustments (relax thresholds for tremor/fatigue):
            tilt  → dead_zone_inner  : 0.05 → 0.08 rad/s (larger tilt dead zone)
        """
        if tilt == self._pain_day_tilt:
            return  # idempotent — no config object created on every tick

        self._pain_day_tilt = tilt
        self._pain_day_active = tilt

        from dataclasses import replace as _dc_replace
        overrides: dict[str, float] = {}
        if tilt:
            overrides["dead_zone_inner"] = 0.08

        self._effective_cfg = _dc_replace(self._cfg, **overrides) if overrides else self._cfg
        log.info("FusionEngine: pain-day thresholds — tilt=%s", tilt)

    # ---------------------------------------------------------------------- #
    # Feature toggle management
    # ---------------------------------------------------------------------- #

    # Valid feature toggle keys. All previous toggles (gaze dwell variants,
    # edge_scroll, gaze_cursor_mode) were gaze features and were removed, so the
    # set is empty: set_feature_toggle now rejects every feature while the bridge
    # message path stays wired without special-casing.
    VALID_FEATURES: set[str] = set()

    def set_feature_toggle(self, feature: str, enabled: bool) -> None:
        """Update a feature toggle. Called by IPadBridge on set_feature_toggle messages."""
        if feature in self.VALID_FEATURES:
            self._feature_toggles[feature] = enabled
            log.info("Feature toggle: %s = %s", feature, enabled)
        else:
            log.warning("Unknown feature toggle: %s", feature)

    # ---------------------------------------------------------------------- #
    # Event callbacks — called from IPadBridge (via asyncio)
    # ---------------------------------------------------------------------- #

    def on_touch(self, cmd: Command) -> None:
        # Pin the click to the current cursor position and request a magnetic
        # snap. A tilt-tap CLICK arrives with no coordinates and text="tilt_tap";
        # without explicit x/y, _resolve_coords would treat "tilt_tap" as a UI
        # target name and run a fuzzy UIAutomation search before falling back to
        # the cursor. Writing x/y is step 1 of the resolver chain, and
        # snap_nearest tells it to magnetically snap those coords to the nearest
        # clickable target within the snap radius — the "area cursor" so coarse
        # tilt positioning is enough and the tap threshold can stay comfortably high.
        if cmd.action == "CLICK" and "x" not in cmd.params:
            px, py = self._cursor_pos
            cmd.params = {**cmd.params, "x": px, "y": py, "snap_nearest": True}
            log.info("FusionEngine: touch CLICK pinned to cursor (%d, %d) [snap]", px, py)
        self._touch = cmd

    def _apply_gravity(self, px_x: int, px_y: int) -> tuple[int, int]:
        """Bias the cursor toward a nearby clickable target (cursor gravity).

        Pulls (px_x, px_y) a little way toward the nearest target's center, by an
        amount that grows as the cursor nears the target's middle (0 at the
        gravity radius, up to _gravity_max_pull at the center). The pull is small
        and never overshoots the center, so it assists rather than hijacks — the
        user can always pull away. Returns the (possibly) adjusted pixel coords.
        """
        cache = self._target_cache
        if cache is None or not cache.is_running():
            return px_x, px_y
        try:
            tg = cache.nearest(px_x, px_y, self._gravity_radius)
        except Exception:
            return px_x, px_y
        if tg is None:
            return px_x, px_y

        cx, cy = tg.center()
        dist = math.hypot(cx - px_x, cy - px_y)
        if dist <= 1.0:
            return cx, cy  # already essentially on it — settle to center
        # Strength: 1.0 at the target center → 0.0 at the gravity radius.
        strength = max(0.0, 1.0 - dist / float(self._gravity_radius))
        pull = min(self._gravity_max_pull, dist) * strength
        nx = px_x + (cx - px_x) * (pull / dist)
        ny = px_y + (cy - px_y) * (pull / dist)
        return round(nx), round(ny)

    def on_tilt(self, rx: float, ry: float) -> None:
        # Reject non-finite values at ingress. JSON parses NaN/Infinity tokens by
        # default, and a single NaN/inf would poison the OneEuroFilter
        # (NaN - NaN = NaN forever) and the sub-pixel accumulator, after which
        # int()/round() in _tick raises every tick until a filter reset — tilt
        # would silently die. Drop the bad frame instead.
        if not (math.isfinite(rx) and math.isfinite(ry)):
            log.debug("FusionEngine: dropping non-finite tilt (%r, %r)", rx, ry)
            return
        self._tilt = (rx, ry)
        self._last_tilt_sample = (rx, ry)  # D2: cache for DB sampling

    def on_tilt_position(self, x: float, y: float) -> None:
        """Receive absolute position from iPad tilt sensor (position-mapped mode)."""
        if not (math.isfinite(x) and math.isfinite(y)):
            log.debug("FusionEngine: dropping non-finite tilt_position (%r, %r)", x, y)
            return
        self._tilt_position = (x, y)

    def on_tilt_ratchet(self) -> None:
        """Handle ratchet trigger from iPad — hold cursor at current position.

        The cursor stays frozen until the next tilt input exceeds the dead zone
        from the new neutral point. The iPad has already captured the new neutral
        gravity vector; this just tells the PC to hold position.
        """
        # Use the cached cursor position (refreshed at 10 Hz by
        # _cursor_cache_loop) rather than a synchronous pyautogui.position() call,
        # which would block the asyncio event loop this callback runs on — the
        # very thing the cursor cache exists to avoid. The held position is
        # informational, so 10 Hz freshness is ample.
        px, py = self._cursor_pos
        self._ratchet_held_pos = (px, py)
        self._ratchet_active = True
        # Reset tilt position filter so it starts fresh from the new neutral
        self._tilt_pos_filter_x.reset()
        self._tilt_pos_filter_y.reset()
        log.info("Ratchet activated — holding cursor at (%d, %d)", px, py)

    def on_sensor_switch(self, from_sensor: str | None, to_sensor: str) -> None:
        """Handle cursor sensor switch — hold cursor for 200ms to prevent jump.

        Called when iPad SensorManager switches the active cursor sensor.
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
            log.info("Cursor resumed (sensors re-zeroed)")

    def on_keyword(self, word: str, conf: float) -> None:
        self._voice_local = word

    def on_gesture(self, cmd: Command) -> None:
        self._gesture = cmd

    def on_voice(self, cmd: Command) -> None:
        self._voice = cmd

    # ---------------------------------------------------------------------- #
    # 60 Hz tick
    # ---------------------------------------------------------------------- #

    async def _cursor_cache_loop(self) -> None:
        """Update _cursor_pos at 10 Hz using a thread so the 60 Hz tick never blocks on position().

        Also re-checks the virtual-desktop geometry ~every 2 s so a resolution
        change or a hot-plugged/removed monitor is picked up without a restart.
        """
        import pyautogui
        _geom_counter = 0
        while self._running:
            try:
                pos = await asyncio.to_thread(pyautogui.position)
                self._cursor_pos = (int(pos.x), int(pos.y))
            except Exception:
                pass
            # Refresh geometry every ~2 s (20 ticks at 10 Hz). Off-loop (ctypes).
            if _geom_counter % 20 == 0:
                try:
                    await asyncio.to_thread(self.refresh_screen_geometry)
                except Exception:
                    pass
            _geom_counter += 1
            await asyncio.sleep(0.1)  # 10 Hz

    async def _tick(self) -> None:
        import pyautogui
        tick_start = time.monotonic()
        cfg = self._effective_cfg  # pain-day-adjusted config alias for this tick

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

        # Rule 2 — Voice keyword "click": click at the CURRENT cursor position.
        # Gaze targeting was removed, so the cursor is wherever tilt / trackpad /
        # touch last put it. Bypass the gates (source="multimodal") and consume
        # the keyword so it never falls through to DICTATE and types "click".
        if self._voice_local and self._voice_local.lower().strip() == "click":
            self._voice_local = None
            px_x, px_y = self._cursor_pos
            cmd = Command(
                text="voice click",
                action="CLICK",
                source="multimodal",
                gaze_coords=(px_x, px_y),
            )
            await self._emit(cmd)
            return

        # --- Pause / Switch hold guard for cursor-driving sensors ---
        # When paused or during sensor switch, discard tilt cursor data without
        # accumulating state.
        if self._cursor_paused or switch_hold_active:
            # Consume and discard cursor sensor inputs
            self._tilt_position = None
            self._tilt = None
            # Reset accumulators to prevent drift buildup
            self._tilt_accum_x = 0.0
            self._tilt_accum_y = 0.0
            # Still allow voice/gesture rules below to fire
            # (they don't move the cursor directly)

        # Rule 4 — Tilt navigation (direct to pyautogui, no Command, no LLM)
        # Position-mapped tilt_position takes priority over legacy velocity tilt.

        # If both tilt_position and legacy tilt are present, use tilt_position and discard legacy
        if self._tilt_position and self._tilt:
            self._tilt = None

        # Rule 4a — Absolute tilt position (position-mapped mode from iPad)
        if self._tilt_position:
            x, y = self._tilt_position
            self._tilt_position = None

            now = time.monotonic()

            # 1-Euro filter (per-axis adaptive smoothing)
            filtered_x = self._tilt_pos_filter_x(x, timestamp=now)
            filtered_y = self._tilt_pos_filter_y(y, timestamp=now)

            # Ratchet: hold cursor until displacement from new neutral exceeds dead zone.
            # When active, suppress tilt movement but fall through to Rules 5-7 so
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

                # Convert to pixels across the FULL virtual desktop so the cursor
                # can reach a side monitor (left/top origin may be negative).
                px_x = self._vleft + round(curved_x * self._vw)
                px_y = self._vtop + round(curved_y * self._vh)

                # Cursor gravity (Phase 3): nudge toward a nearby clickable so the
                # cursor 'sticks' to buttons. Cheap — pure-Python nearest() over
                # the cached snapshot. No-op when no target is near.
                px_x, px_y = self._apply_gravity(px_x, px_y)

                # Clamp to the virtual-desktop bounds
                px_x = max(self._vleft, min(self._vleft + self._vw - 1, px_x))
                px_y = max(self._vtop, min(self._vtop + self._vh - 1, px_y))

                await asyncio.to_thread(pyautogui.moveTo, px_x, px_y, duration=0)
                return
            # Ratchet hold: no cursor movement — fall through to Rules 5-7

        # Rule 4b — Legacy tilt navigation (velocity-based, no Command, no LLM)
        if self._tilt:
            rx, ry = self._tilt
            self._tilt = None

            now = time.monotonic()

            # 1. Feed raw values to bias calibrator (state machine update)
            self._tilt_bias_cal.update(rx, ry, now=now)

            # 2. Subtract estimated gyro bias
            bias_x, bias_y = self._tilt_bias_cal.get_current_bias(now=now)
            rx -= bias_x
            ry -= bias_y

            # 3. Check uncalibrated suppression — fall through to Rules 5–7 if suppressed
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

                # Clamp per-frame displacement so a tremor spike (amplified by
                # the quadratic power curve) can't fling the cursor across the
                # screen in a single frame.
                _max_px = self._effective_cfg.tilt_max_px_per_frame
                if _max_px > 0:
                    cursor_dx = max(-_max_px, min(_max_px, cursor_dx))
                    cursor_dy = max(-_max_px, min(_max_px, cursor_dy))

                # rx = rotationRate.x: rotation around X-axis → vertical cursor
                #   positive rx (tilt top away) → cursor moves up (negative dy)
                # ry = rotationRate.y: rotation around Y-axis → horizontal cursor
                #   tilt right (negative ry) → cursor moves right (positive dx)
                self._tilt_accum_x += -cursor_dy  # ry → horizontal
                self._tilt_accum_y += -cursor_dx  # rx → vertical

                # Only move when we've accumulated at least 1 pixel.
                # BUG FIX: return ONLY when pixels were produced — otherwise fall
                # through to Rules 5–7 so voice/gesture fire while tilt is sub-dead-zone.
                dx = int(self._tilt_accum_x)
                dy = int(self._tilt_accum_y)

                if dx or dy:
                    self._tilt_accum_x -= dx
                    self._tilt_accum_y -= dy
                    await asyncio.to_thread(pyautogui.moveRel, dx, dy, duration=0)
                    return  # cursor moved — this tick is done
            # Suppressed or sub-dead-zone: fall through to Rules 5–7

        # Rule 5 — Gesture (full 4-gate)
        if self._gesture:
            cmd, self._gesture = self._gesture, None
            await self._emit(cmd)
            return

        # Rule 6 — On-device voice keyword, skip Gate 1
        if self._voice_local:
            word, self._voice_local = self._voice_local, None
            cmd = Command(text=word, action="DICTATE", source="voice_local")
            await self._emit(cmd)
            return

        # Rule 7 — PC-transcribed voice, full 4-gate
        if self._voice:
            cmd, self._voice = self._voice, None
            await self._emit(cmd)

        # Telemetry sampler: ~1 Hz rich snapshot (every 60 ticks at 60 Hz).
        # Writes to sensor_telemetry table — one row captures the active sensor
        # channels plus cursor position and pain-day state. This is the primary
        # dataset for future ML (fatigue detection, ROM drift, pain-onset classifiers).
        # Fire-and-forget: DB writes must never stall the 60 Hz tick loop.
        # Gaze/head columns remain in the schema for backward compatibility but are
        # always None now that those sensors are removed.
        if self._db is not None and self._db.available and self._tick_count % 60 == 0:
            _tilt_rx = _tilt_ry = None
            _cursor_x = _cursor_y = None
            _rms = None

            if self._last_tilt_sample is not None:
                _tilt_rx, _tilt_ry = self._last_tilt_sample
            if self._acoustic_profiler is not None:
                _rms = getattr(self._acoustic_profiler, "_last_rms", None)

            # Cursor position from cache (updated at 10 Hz by _cursor_cache_loop)
            _cursor_x, _cursor_y = self._cursor_pos

            fire_and_log(
                self._db.insert_sensor_telemetry(
                    self._session_id,
                    time.time(),
                    tilt_rx=_tilt_rx,
                    tilt_ry=_tilt_ry,
                    gaze_dx=None,
                    gaze_dy=None,
                    gaze_conf=None,
                    head_pitch=None,
                    head_yaw=None,
                    cursor_x=_cursor_x,
                    cursor_y=_cursor_y,
                    pain_day_active=self._pain_day_active,
                    active_source=self._last_active_source,
                    gesture_conf=self._last_gesture_conf,
                    rms_ambient=_rms,
                ),
                log, "sensor_telemetry write",
            )
            # Also push legacy sensor_events for backward-compat DuckDB queries
            if self._last_tilt_sample is not None:
                fire_and_log(
                    self._db.insert_sensor_event("tilt", x=_tilt_rx, y=_tilt_ry),
                    log, "sensor_event write",
                )

    async def _emit(self, cmd: Command) -> None:
        # Track last active source for telemetry and metrics
        self._last_active_source = cmd.source
        if cmd.source == "gesture" and cmd.gesture_confidence is not None:
            self._last_gesture_conf = cmd.gesture_confidence
        if self._metrics is not None:
            self._metrics.record_command_routed(cmd.source)
        if self._coordinator:
            # Cross-layer trace: stamp the command at its birth (no-op unless
            # DA_TRACE is on) so the id survives the scheduler create_task hop.
            from monitoring.trace import get_tracer
            _tracer = get_tracer()
            if _tracer.enabled and not cmd.trace_id:
                cmd.trace_id = _tracer.new_trace(source=cmd.source)
                _tracer.record_span("enqueue", trace_id=cmd.trace_id, source=cmd.source)
            if self._scheduler is not None:
                # Priority-aware dispatch: DEV_AGENT/BACKGROUND tasks are gated
                # so they cannot starve accessibility commands during a flare.
                priority = self._source_to_priority(cmd.source)
                self._scheduler.submit(
                    self._coordinator.route(cmd),
                    priority=priority,
                    label=cmd.source,
                    trace_id=cmd.trace_id,
                )
            else:
                # Fallback: bare fire-and-forget (scheduler not yet wired)
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
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def run(self) -> None:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0

        self._running = True
        # Pick up the real multi-monitor layout before the first tilt frame so
        # absolute positioning can reach a side monitor immediately.
        try:
            await asyncio.to_thread(self.refresh_screen_geometry)
        except Exception:
            pass
        self._cursor_cache_task = asyncio.create_task(
            self._cursor_cache_loop(), name="cursor_cache"
        )
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
        if self._cursor_cache_task is not None:
            self._cursor_cache_task.cancel()
            self._cursor_cache_task = None
        log.info("FusionEngine stopped")
