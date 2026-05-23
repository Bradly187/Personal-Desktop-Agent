"""Tests for ActionVerifier — Sprint 7 perceptual-diff action verification."""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png_b64(color: tuple = (255, 255, 255), size: tuple = (10, 10)) -> str:
    """Return a solid-color PNG as base64. Returns '' if Pillow unavailable."""
    try:
        from PIL import Image
        img = Image.new("RGB", size, color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        return ""


def _requires_pillow(av=None):
    """Skip the test if Pillow is unavailable or the verifier says so."""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed")
    if av is not None and not av._available:
        pytest.skip("ActionVerifier: Pillow not installed")


# ---------------------------------------------------------------------------
# VerifyResult dataclass
# ---------------------------------------------------------------------------

class TestVerifyResult:
    def test_default_optional_fields(self):
        from action_verifier import VerifyResult
        r = VerifyResult(success=True, diff_pct=5.5, reason="changed")
        assert r.pre_b64 == ""
        assert r.post_b64 == ""
        assert r.elapsed_ms == 0.0

    def test_explicit_fields_stored(self):
        from action_verifier import VerifyResult
        r = VerifyResult(
            success=False, diff_pct=0.0, reason="no_change",
            pre_b64="aaa", post_b64="bbb", elapsed_ms=120.5,
        )
        assert r.success is False
        assert r.diff_pct == pytest.approx(0.0)
        assert r.pre_b64 == "aaa"
        assert r.post_b64 == "bbb"
        assert r.elapsed_ms == pytest.approx(120.5)


# ---------------------------------------------------------------------------
# Skip paths — no Pillow or non-verifiable verb
# ---------------------------------------------------------------------------

class TestActionVerifierSkip:
    def test_type_verb_skips(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        result = av.verify("somebase64", "TYPE")
        assert result.success is True
        assert result.reason == "skip"

    def test_hotkey_verb_skips(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        result = av.verify("somebase64", "HOTKEY")
        assert result.reason == "skip"

    def test_dictate_verb_skips(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        result = av.verify("somebase64", "DICTATE")
        assert result.reason == "skip"

    def test_empty_pre_b64_skips(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        result = av.verify("", "CLICK")
        assert result.reason == "skip"

    def test_unavailable_skips(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        av._available = False
        result = av.verify("somebase64", "CLICK")
        assert result.reason == "skip"


# ---------------------------------------------------------------------------
# Error path — post-snapshot failure
# ---------------------------------------------------------------------------

class TestActionVerifierError:
    def test_failed_post_snapshot_returns_error(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        pre = _png_b64((255, 255, 255))
        # Pass empty post_b64="" to trigger internal snapshot() call;
        # patch time.sleep to avoid the 0.4s delay, patch snapshot to fail.
        with patch("action_verifier.time.sleep"), \
             patch.object(av, "snapshot", return_value=""):
            result = av.verify(pre, "CLICK", post_b64="")

        assert result.success is False
        assert result.reason == "error"


# ---------------------------------------------------------------------------
# _diff — pure Pillow, no OS interaction
# ---------------------------------------------------------------------------

class TestActionVerifierDiff:
    def test_identical_images_diff_zero(self):
        _requires_pillow()
        from action_verifier import ActionVerifier
        white = _png_b64((255, 255, 255))
        assert ActionVerifier._diff(white, white) == pytest.approx(0.0)

    def test_fully_different_images_high_diff(self):
        _requires_pillow()
        from action_verifier import ActionVerifier
        white = _png_b64((255, 255, 255))
        black = _png_b64((0, 0, 0))
        diff = ActionVerifier._diff(white, black)
        assert diff > 90.0

    def test_different_sizes_resampled_without_error(self):
        _requires_pillow()
        from action_verifier import ActionVerifier
        small = _png_b64((255, 255, 255), size=(5, 5))
        large = _png_b64((0, 0, 0), size=(20, 20))
        diff = ActionVerifier._diff(small, large)
        assert isinstance(diff, float)
        assert diff >= 0.0

    def test_noise_below_10_per_channel_not_counted(self):
        """Pixel differences ≤10 per channel must be ignored (JPEG noise floor)."""
        _requires_pillow()
        from action_verifier import ActionVerifier
        from PIL import Image

        img1 = Image.new("RGB", (10, 10), (128, 128, 128))
        img2 = Image.new("RGB", (10, 10), (133, 133, 133))  # delta=5, below threshold

        def to_b64(img: Image.Image) -> str:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()

        assert ActionVerifier._diff(to_b64(img1), to_b64(img2)) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# verify() — end-to-end with synthetic images
# ---------------------------------------------------------------------------

class TestActionVerifierVerify:
    def test_changed_image_returns_success(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        pre = _png_b64((255, 255, 255))
        post = _png_b64((0, 0, 0))
        result = av.verify(pre, "CLICK", post_b64=post)

        assert result.success is True
        assert result.reason == "changed"
        assert result.diff_pct > 2.0

    def test_unchanged_image_returns_no_change(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        white = _png_b64((255, 255, 255))
        result = av.verify(white, "CLICK", post_b64=white)

        assert result.success is False
        assert result.reason == "no_change"
        assert result.diff_pct == pytest.approx(0.0)

    def test_open_verb_is_verifiable(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        pre = _png_b64((200, 200, 200))
        post = _png_b64((100, 100, 100))
        result = av.verify(pre, "OPEN", post_b64=post)
        assert result.reason == "changed"

    def test_scroll_verb_is_verifiable(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        white = _png_b64((255, 255, 255))
        result = av.verify(white, "SCROLL", post_b64=white)
        assert result.reason == "no_change"

    def test_verb_case_insensitive(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        pre = _png_b64((255, 255, 255))
        post = _png_b64((0, 0, 0))
        result = av.verify(pre, "click", post_b64=post)  # lowercase
        assert result.reason != "skip"
        assert result.success is True

    def test_close_verb_is_verifiable(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        pre = _png_b64((255, 128, 0))
        post = _png_b64((0, 0, 255))
        result = av.verify(pre, "CLOSE", post_b64=post)
        assert result.reason == "changed"

    def test_elapsed_ms_populated(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        _requires_pillow(av)

        white = _png_b64((255, 255, 255))
        result = av.verify(white, "CLICK", post_b64=white)
        assert result.elapsed_ms >= 0.0

    def test_get_status_keys(self):
        from action_verifier import ActionVerifier
        av = ActionVerifier()
        status = av.get_status()
        assert "available" in status
        assert "verifiable_verbs" in status
        assert "change_threshold_pct" in status
        assert "verify_delay_s" in status

    def test_get_status_threshold_matches_constant(self):
        from action_verifier import ActionVerifier, CHANGE_THRESHOLD
        av = ActionVerifier()
        assert av.get_status()["change_threshold_pct"] == pytest.approx(CHANGE_THRESHOLD * 100)

    def test_get_status_verifiable_verbs_sorted(self):
        from action_verifier import ActionVerifier, VERIFIABLE_VERBS
        av = ActionVerifier()
        assert set(av.get_status()["verifiable_verbs"]) == VERIFIABLE_VERBS
