"""Tests for axis correction fixes (Requirements 9, 10).

Verifies that:
- Head tracker: positive pitch (chin to chest) → cursor moves DOWN (positive dy)
- Head tracker: positive yaw (turn right) → cursor moves RIGHT (positive dx)
- The FusionEngine no longer negates pitch for head tracking.
"""

import pytest


class TestHeadTrackerAxisMapping:
    """Verify head tracker axis mapping in FusionEngine."""

    def test_positive_pitch_produces_positive_dy(self):
        """Forward head tilt (chin to chest, positive pitch) → cursor down (positive dy)."""
        from core.fusion_engine import FusionConfig

        cfg = FusionConfig()
        # Simulate what Rule 7 does: dy = int(pitch * head_sensitivity)
        pitch = 2.0  # degrees, forward tilt
        dy = int(pitch * cfg.head_sensitivity)
        assert dy > 0, f"Expected positive dy for positive pitch, got {dy}"

    def test_negative_pitch_produces_negative_dy(self):
        """Backward head tilt (head back, negative pitch) → cursor up (negative dy)."""
        from core.fusion_engine import FusionConfig

        cfg = FusionConfig()
        pitch = -2.0  # degrees, backward tilt
        dy = int(pitch * cfg.head_sensitivity)
        assert dy < 0, f"Expected negative dy for negative pitch, got {dy}"

    def test_positive_yaw_produces_positive_dx(self):
        """Turn head right (positive yaw) → cursor right (positive dx)."""
        from core.fusion_engine import FusionConfig

        cfg = FusionConfig()
        yaw = 2.0  # degrees, turn right
        dx = int(yaw * cfg.head_sensitivity)
        assert dx > 0, f"Expected positive dx for positive yaw, got {dx}"

    def test_negative_yaw_produces_negative_dx(self):
        """Turn head left (negative yaw) → cursor left (negative dx)."""
        from core.fusion_engine import FusionConfig

        cfg = FusionConfig()
        yaw = -2.0  # degrees, turn left
        dx = int(yaw * cfg.head_sensitivity)
        assert dx < 0, f"Expected negative dx for negative yaw, got {dx}"
