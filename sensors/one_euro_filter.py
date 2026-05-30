"""1-Euro adaptive low-pass filter (Casiez et al., 2012).

Provides jitter reduction at low speeds and minimal latency at high speeds.
Used in FusionEngine for tilt velocity and tilt position pipelines.

Reference: Géry Casiez, Nicolas Roussel, Daniel Vogel. 1€ Filter: A Simple
Speed-based Low-pass Filter for Noisy Input in Interactive Systems. CHI 2012.
"""

from __future__ import annotations

import logging
import math
import time

log = logging.getLogger(__name__)


class OneEuroFilter:
    """Adaptive low-pass filter that adjusts cutoff frequency based on signal speed.

    At low speeds: heavy smoothing (jitter reduction).
    At high speeds: raised cutoff (minimal latency).

    Parameters:
        min_cutoff: Minimum cutoff frequency in Hz (default 1.0, range [0.1, 10.0]).
            Lower values = more smoothing at rest.
        beta: Speed coefficient (default 0.007, range [0.0, 1.0]).
            Higher values = faster response to speed changes.
        d_cutoff: Derivative cutoff frequency in Hz (default 1.0, range [0.1, 10.0]).
            Controls smoothing of the speed estimate itself.
    """

    def __init__(
        self,
        min_cutoff: float = 1.0,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ) -> None:
        # Clamp parameters to valid ranges
        if not (0.1 <= min_cutoff <= 10.0):
            log.warning(
                "OneEuroFilter: min_cutoff %.4f outside [0.1, 10.0], clamping",
                min_cutoff,
            )
        if not (0.0 <= beta <= 1.0):
            log.warning(
                "OneEuroFilter: beta %.4f outside [0.0, 1.0], clamping", beta
            )
        if not (0.1 <= d_cutoff <= 10.0):
            log.warning(
                "OneEuroFilter: d_cutoff %.4f outside [0.1, 10.0], clamping",
                d_cutoff,
            )

        self.min_cutoff = max(0.1, min(10.0, min_cutoff))
        self.beta = max(0.0, min(1.0, beta))
        self.d_cutoff = max(0.1, min(10.0, d_cutoff))

        self._x_prev: float | None = None
        self._dx_prev: float = 0.0
        self._t_prev: float | None = None

    def reset(self, initial_value: float | None = None) -> None:
        """Reset filter state. Optionally seed with an initial value."""
        self._x_prev = initial_value
        self._dx_prev = 0.0
        self._t_prev = None

    def __call__(self, x: float, timestamp: float | None = None) -> float:
        """Filter a single sample. Returns the filtered value.

        Args:
            x: Raw input value.
            timestamp: Monotonic time of this sample. If None, uses time.monotonic().

        Returns:
            Filtered output value.
        """
        now = timestamp if timestamp is not None else time.monotonic()

        if self._t_prev is None or self._x_prev is None:
            # First sample — initialize without jump
            self._x_prev = x
            self._dx_prev = 0.0
            self._t_prev = now
            return x

        dt = now - self._t_prev
        if dt <= 0:
            # Same or earlier timestamp — return previous output
            return self._x_prev
        self._t_prev = now

        # 1. Compute raw derivative
        dx = (x - self._x_prev) / dt

        # 2. Filter derivative with fixed cutoff
        alpha_d = self._alpha(self.d_cutoff, dt)
        self._dx_prev = alpha_d * dx + (1 - alpha_d) * self._dx_prev

        # 3. Compute adaptive cutoff frequency
        fc = self.min_cutoff + self.beta * abs(self._dx_prev)

        # 4. Filter signal with adaptive cutoff
        alpha = self._alpha(fc, dt)
        self._x_prev = alpha * x + (1 - alpha) * self._x_prev

        return self._x_prev

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        """Compute smoothing factor alpha from cutoff frequency and time step."""
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    @property
    def initialized(self) -> bool:
        """Whether the filter has received at least one sample."""
        return self._x_prev is not None
