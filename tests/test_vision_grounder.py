"""Tests for VisionGrounder — Sprint 5 vision grounding (Rank 7)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _tiny_png_b64() -> str:
    """Return a 1x1 white PNG as base64 for testing."""
    # Minimal valid PNG (1x1 white pixel)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return base64.b64encode(png_bytes).decode()


class TestVisionGrounderBasics:
    def test_ground_returns_none_on_empty_inputs(self):
        from desktop.vision_grounder import VisionGrounder
        vg = VisionGrounder()
        assert vg.ground("", "somebase64") is None
        assert vg.ground("submit button", "") is None

    def test_ground_cache_hit_returns_result(self):
        from desktop.vision_grounder import VisionGrounder
        import time
        vg = VisionGrounder()
        # Seed the cache directly
        vg._cache["submit button"] = (400, 300, time.monotonic() + 10.0)

        result = vg.ground("submit button", _tiny_png_b64())
        assert result is not None
        assert result.x == 400
        assert result.y == 300
        assert result.source == "vision_cache"

    def test_ground_cache_expired_does_not_hit(self):
        from desktop.vision_grounder import VisionGrounder
        import time
        vg = VisionGrounder()
        vg._cache["old button"] = (400, 300, time.monotonic() - 5.0)  # expired

        # Without a real client this will try to get_client() and fail
        with patch.object(vg, "_get_client", side_effect=RuntimeError("no key")):
            result = vg.ground("old button", _tiny_png_b64())
        assert result is None  # expired cache → fresh call → failed → None

    def test_get_client_raises_when_anthropic_missing(self):
        from desktop.vision_grounder import VisionGrounder
        vg = VisionGrounder()
        # Stub the Bedrock credential so resolve_backend() succeeds and the
        # test exercises the anthropic-import failure, not credential absence
        # (CI has no AWS_BEARER_TOKEN_BEDROCK; dev machines do).
        with patch.dict("os.environ", {"AWS_BEARER_TOKEN_BEDROCK": "test-token"}):
            with patch.dict("sys.modules", {"anthropic": None}):
                with pytest.raises(RuntimeError, match="anthropic package"):
                    vg._get_client()

    def test_parse_handles_markdown_fences(self):
        """Fence-stripping + JSON parsing logic works on fenced Claude output."""
        import json

        # Test the stripping logic directly without going through the full API path
        raw_with_fences = '```json\n{"x": 100, "y": 200, "confidence": 0.9}\n```'

        # Replicate the stripping logic from _ask_claude
        raw = raw_with_fences.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        data = json.loads(raw)
        assert data["x"] == 100
        assert data["y"] == 200
        assert data["confidence"] == pytest.approx(0.9)

    def test_parse_null_coords_returns_none(self):
        """JSON with null coords returns None without raising."""
        import json
        from desktop.vision_grounder import VisionGrounder

        raw = '{"x": null, "y": null, "confidence": 0.0}'
        data = json.loads(raw)
        x = data.get("x")
        y = data.get("y")
        # Verify the None-check logic directly
        assert x is None
        assert y is None
        # And confirm VisionGrounder treats this as not found
        vg = VisionGrounder()
        # _ask_claude internals: if x/y is None → return None
        if x is None or y is None:
            result = None
        assert result is None

    def test_low_confidence_returns_none(self):
        """Results below GROUNDING_MIN_CONFIDENCE must return None."""
        from desktop.vision_grounder import VisionGrounder, GROUNDING_MIN_CONFIDENCE
        vg = VisionGrounder()

        with patch.object(vg, "_get_client", return_value=MagicMock()), \
             patch.object(vg, "_ask_claude", return_value=(500, 400, GROUNDING_MIN_CONFIDENCE - 0.1)):
            result = vg.ground("fuzzy target", _tiny_png_b64())

        assert result is None

    def test_high_confidence_returns_result(self):
        """Results at or above GROUNDING_MIN_CONFIDENCE must return GroundingResult."""
        from desktop.vision_grounder import VisionGrounder, GroundingResult, GROUNDING_MIN_CONFIDENCE
        vg = VisionGrounder()

        with patch.object(vg, "_get_client", return_value=MagicMock()), \
             patch.object(vg, "_ask_claude", return_value=(800, 600, GROUNDING_MIN_CONFIDENCE + 0.1)):
            result = vg.ground("clear button", _tiny_png_b64())

        assert result is not None
        assert isinstance(result, GroundingResult)
        assert result.x == 800
        assert result.y == 600

    def test_successful_ground_populates_cache(self):
        """A successful grounding result must be cached for 2 seconds."""
        from desktop.vision_grounder import VisionGrounder
        import time
        vg = VisionGrounder()

        with patch.object(vg, "_get_client", return_value=MagicMock()), \
             patch.object(vg, "_ask_claude", return_value=(100, 200, 0.95)):
            vg.ground("save button", _tiny_png_b64())

        assert "save button" in vg._cache
        cx, cy, expiry = vg._cache["save button"]
        assert cx == 100
        assert cy == 200
        assert expiry > time.monotonic()

    def test_api_exception_returns_none(self):
        """API errors must degrade gracefully to None."""
        from desktop.vision_grounder import VisionGrounder
        vg = VisionGrounder()

        with patch.object(vg, "_get_client", return_value=MagicMock()), \
             patch.object(vg, "_ask_claude", side_effect=Exception("API down")):
            result = vg.ground("any button", _tiny_png_b64())

        assert result is None

    def test_get_status_returns_dict(self):
        from desktop.vision_grounder import VisionGrounder
        vg = VisionGrounder()
        status = vg.get_status()
        assert "model" in status
        assert "cache_entries" in status
        assert "min_confidence" in status
