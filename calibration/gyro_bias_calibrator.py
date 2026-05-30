"""Continuous gyro bias calibration for tilt velocity mode.

Detects stationary periods and estimates the gyroscope's zero-rate offset,
then subtracts it from subsequent readings to prevent cursor drift.

State machine:
  UNCALIBRATED → COLLECTING → CALIBRATED ↔ FROZEN
"""

from __future__ import annotations

import logging
import time
from enum import Enum

log = logging.getLogger(__name__)


class BiasState(Enum):
    """Gyro bias calibrator states."""

    UNCALIBRATED = "uncalibrated"
    COLLECTING = "collecting"
    CALIBRATED = "calibrated"
    FROZEN = "frozen"


class GyroBiasCalibrator:
    """Estimates and subtracts gyroscope bias for tilt velocity mode.

    Detects when the device is stationary (angular velocity below threshold
    on all axes for a duration), collects samples, averages them as the bias
    estimate, and smoothly transitions to the new bias.

    Parameters:
        stationary_threshold: Max angular velocity (rad/s) to consider stationary.
        stationary_duration: Seconds of stillness before collecting begins.
        min_samples: Minimum samples to collect before computing bias.
        max_samples: Maximum samples to collect (stops after this).
        lerp_duration: Seconds to interpolate from old to new bias.
        uncalibrated_suppress: Suppress velocities below this when uncalibrated.
    """

    def __init__(
        self,
        stationary_threshold: float = 0.02,
        stationary_duration: float = 1.0,
        min_samples: int = 50,
        max_samples: int = 200,
        lerp_duration: float = 0.5,
        uncalibrated_suppress: float = 0.05,
    ) -> None:
        self.stationary_threshold = stationary_threshold
        self.stationary_duration = stationary_duration
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.lerp_duration = lerp_duration
        self.uncalibrated_suppress = uncalibrated_suppress

        self.state = BiasState.UNCALIBRATED
        self.bias_x: float = 0.0
        self.bias_y: float = 0.0

        # Lerp transition state
        self._target_bias_x: float = 0.0
        self._target_bias_y: float = 0.0
        self._lerp_start_time: float | None = None
        self._lerp_start_bias: tuple[float, float] = (0.0, 0.0)

        # Stationary detection
        self._stationary_start: float | None = None

        # Sample collection
        self._samples_x: list[float] = []
        self._samples_y: list[float] = []

    def reset(self) -> None:
        """Reset to uncalibrated state."""
        self.state = BiasState.UNCALIBRATED
        self.bias_x = 0.0
        self.bias_y = 0.0
        self._target_bias_x = 0.0
        self._target_bias_y = 0.0
        self._lerp_start_time = None
        self._lerp_start_bias = (0.0, 0.0)
        self._stationary_start = None
        self._samples_x.clear()
        self._samples_y.clear()

    def update(self, rx: float, ry: float, now: float | None = None) -> None:
        """Feed a rotation rate sample and advance the state machine.

        Call this every frame with the raw (pre-bias-subtraction) rotation rates.

        Args:
            rx: Rotation rate around X axis (rad/s).
            ry: Rotation rate around Y axis (rad/s).
            now: Monotonic timestamp. If None, uses time.monotonic().
        """
        if now is None:
            now = time.monotonic()

        is_stationary = abs(rx) < self.stationary_threshold and abs(ry) < self.stationary_threshold

        if self.state == BiasState.UNCALIBRATED:
            if is_stationary:
                if self._stationary_start is None:
                    self._stationary_start = now
                elif now - self._stationary_start >= self.stationary_duration:
                    # Transition to COLLECTING
                    self.state = BiasState.COLLECTING
                    self._samples_x.clear()
                    self._samples_y.clear()
                    self._samples_x.append(rx)
                    self._samples_y.append(ry)
                    log.info("GyroBiasCalibrator: stationary detected, collecting samples")
            else:
                self._stationary_start = None

        elif self.state == BiasState.COLLECTING:
            if is_stationary:
                self._samples_x.append(rx)
                self._samples_y.append(ry)
                if len(self._samples_x) >= self.max_samples:
                    self._finalize_calibration(now)
            else:
                # Motion interrupted collection
                if len(self._samples_x) >= self.min_samples:
                    # Enough samples — finalize
                    self._finalize_calibration(now)
                else:
                    # Not enough — go back to detecting
                    self.state = BiasState.UNCALIBRATED
                    self._stationary_start = None
                    self._samples_x.clear()
                    self._samples_y.clear()

        elif self.state == BiasState.CALIBRATED:
            if not is_stationary:
                # Motion detected — transition to FROZEN
                self._stationary_start = None
                self.state = BiasState.FROZEN
            # While stationary in CALIBRATED, do nothing — wait for motion
            # to trigger FROZEN, then a new stationary period triggers recalibration.

        elif self.state == BiasState.FROZEN:
            if is_stationary:
                if self._stationary_start is None:
                    self._stationary_start = now
                elif now - self._stationary_start >= self.stationary_duration:
                    # Back to collecting
                    self.state = BiasState.COLLECTING
                    self._samples_x.clear()
                    self._samples_y.clear()
                    self._samples_x.append(rx)
                    self._samples_y.append(ry)
            else:
                self._stationary_start = None

    def _finalize_calibration(self, now: float) -> None:
        """Compute bias from collected samples and start lerp transition."""
        new_bias_x = sum(self._samples_x) / len(self._samples_x)
        new_bias_y = sum(self._samples_y) / len(self._samples_y)

        # Check if bias changed significantly
        delta = max(abs(new_bias_x - self.bias_x), abs(new_bias_y - self.bias_y))

        if delta > 0.005:
            # Smooth transition
            self._lerp_start_time = now
            self._lerp_start_bias = (self.bias_x, self.bias_y)
            self._target_bias_x = new_bias_x
            self._target_bias_y = new_bias_y
            log.info(
                "GyroBiasCalibrator: new bias (%.5f, %.5f) → lerping over %.1fs",
                new_bias_x,
                new_bias_y,
                self.lerp_duration,
            )
        else:
            # Small change — apply immediately
            self.bias_x = new_bias_x
            self.bias_y = new_bias_y

        self.state = BiasState.CALIBRATED
        self._stationary_start = None
        self._samples_x.clear()
        self._samples_y.clear()

    def get_current_bias(self, now: float | None = None) -> tuple[float, float]:
        """Get the current effective bias, accounting for lerp transitions.

        Args:
            now: Monotonic timestamp. If None, uses time.monotonic().

        Returns:
            Tuple (bias_x, bias_y) in rad/s.
        """
        if now is None:
            now = time.monotonic()

        if self._lerp_start_time is None:
            return (self.bias_x, self.bias_y)

        elapsed = now - self._lerp_start_time
        t = min(1.0, elapsed / self.lerp_duration)

        bx = self._lerp_start_bias[0] + t * (self._target_bias_x - self._lerp_start_bias[0])
        by = self._lerp_start_bias[1] + t * (self._target_bias_y - self._lerp_start_bias[1])

        if t >= 1.0:
            # Lerp complete
            self.bias_x = self._target_bias_x
            self.bias_y = self._target_bias_y
            self._lerp_start_time = None

        return (bx, by)

    def should_suppress(self, rx: float, ry: float) -> bool:
        """Check if velocity should be suppressed (uncalibrated state).

        When no calibration has been performed yet, suppress small velocities
        that are likely bias-induced drift.

        Args:
            rx: Bias-subtracted rotation rate X.
            ry: Bias-subtracted rotation rate Y.

        Returns:
            True if the velocity should be suppressed (output zero).
        """
        if self.state == BiasState.UNCALIBRATED:
            return (
                abs(rx) < self.uncalibrated_suppress
                and abs(ry) < self.uncalibrated_suppress
            )
        return False
