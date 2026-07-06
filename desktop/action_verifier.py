"""desktop.action_verifier.py — Perceptual diff verification after desktop actions.

After every CLICK, OPEN, CLOSE, or SCROLL command, takes a screenshot and
compares it to the pre-action state.  If the screen changed by more than
CHANGE_THRESHOLD (2% pixel area), the action is considered successful.
Failed verifications emit a CLARIFY and feed a failure signal to
BehavioralTwinState for pain-day scoring.

Usage (wired by CommandExecutor.execute):
    verifier = ActionVerifier()
    pre_b64  = verifier.snapshot()          # before action
    ...execute action...
    result   = await asyncio.sleep(0.4)     # wait for animation
    outcome  = verifier.verify(pre_b64, action)
    if not outcome.success:
        log.warning("Action verification failed: %s", outcome.reason)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Verifiable action verbs — TYPE and HOTKEY produce no visible diff
VERIFIABLE_VERBS: frozenset[str] = frozenset({"CLICK", "OPEN", "CLOSE", "SCROLL"})

# Fraction of pixels that must differ to count as "screen changed"
CHANGE_THRESHOLD: float = 0.02

# Seconds to wait after action before taking the verification screenshot
VERIFY_DELAY_S: float = 0.4

# Store screenshots in DB for first N days, then drop to outcome flag only
SCREENSHOT_RETENTION_DAYS: int = 30


@dataclass
class VerifyResult:
    success: bool
    diff_pct: float          # 0.0–100.0
    reason: str              # "changed" | "no_change" | "skip" | "error"
    pre_b64:  str = ""       # base64 PNG (stored to DB)
    post_b64: str = ""       # base64 PNG (stored to DB)
    elapsed_ms: float = 0.0


class ActionVerifier:
    """Perceptual diff screen verification using Pillow ImageChops."""

    def __init__(self) -> None:
        self._available = self._check_deps()

    @staticmethod
    def _check_deps() -> bool:
        try:
            from PIL import Image, ImageChops  # noqa: F401
            return True
        except ImportError:
            log.warning("ActionVerifier: Pillow not installed — verification disabled")
            return False

    # ── Public API ─────────────────────────────────────────────────────────

    def snapshot(self) -> str:
        """Take a full-screen PNG and return it as base64. Blocking."""
        if not self._available:
            return ""
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).parent / "mcp_server"))
            from tools import screen  # type: ignore[import-not-found,import-untyped]
            result = screen.screenshot()
            return result.get("image_base64", "")
        except Exception as exc:
            log.debug("ActionVerifier.snapshot failed: %s", exc)
            return ""

    def verify(
        self,
        pre_b64: str,
        action: str,
        post_b64: str = "",
    ) -> VerifyResult:
        """Compare pre and post screenshots; return a VerifyResult. Blocking.

        If post_b64 is not supplied, a new screenshot is taken automatically
        (after VERIFY_DELAY_S). Call via asyncio.to_thread.
        """
        t0 = time.monotonic()

        if action.upper() not in VERIFIABLE_VERBS:
            return VerifyResult(
                success=True, diff_pct=0.0, reason="skip",
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        if not self._available or not pre_b64:
            return VerifyResult(
                success=True, diff_pct=0.0, reason="skip",
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        if not post_b64:
            time.sleep(VERIFY_DELAY_S)
            post_b64 = self.snapshot()

        if not post_b64:
            log.warning("ActionVerifier: post-action snapshot failed — cannot verify")
            return VerifyResult(
                success=False, diff_pct=0.0, reason="error",
                pre_b64=pre_b64, post_b64="",
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        try:
            diff_pct = self._diff(pre_b64, post_b64)
        except Exception as exc:
            log.warning("ActionVerifier._diff failed: %s", exc)
            return VerifyResult(
                success=True, diff_pct=0.0, reason="error",
                pre_b64=pre_b64, post_b64=post_b64,
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        success = diff_pct >= CHANGE_THRESHOLD * 100
        reason = "changed" if success else "no_change"

        if not success:
            log.info(
                "ActionVerifier: %s — no visible change (diff=%.2f%% < %.0f%%)",
                action, diff_pct, CHANGE_THRESHOLD * 100,
            )

        return VerifyResult(
            success=success,
            diff_pct=round(diff_pct, 2),
            reason=reason,
            pre_b64=pre_b64,
            post_b64=post_b64,
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
        )

    # ── Perceptual diff ─────────────────────────────────────────────────────

    @staticmethod
    def _diff(pre_b64: str, post_b64: str) -> float:
        """Return percentage of pixels that changed between two PNG images."""
        from PIL import Image, ImageChops
        import io

        pre_bytes  = base64.b64decode(pre_b64)
        post_bytes = base64.b64decode(post_b64)

        pre_img  = Image.open(io.BytesIO(pre_bytes)).convert("RGB")
        post_img = Image.open(io.BytesIO(post_bytes)).convert("RGB")

        # Resize post to pre dimensions if they differ (window resize edge case)
        if pre_img.size != post_img.size:
            post_img = post_img.resize(pre_img.size, Image.LANCZOS)

        diff = ImageChops.difference(pre_img, post_img)

        # Count pixels with any non-zero difference channel
        total = pre_img.width * pre_img.height
        if total == 0:
            return 0.0

        # Threshold diff at 10/255 to ignore JPEG compression noise
        import struct
        changed = sum(
            1 for r, g, b in diff.getdata()
            if r > 10 or g > 10 or b > 10
        )
        return (changed / total) * 100.0

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "verifiable_verbs": sorted(VERIFIABLE_VERBS),
            "change_threshold_pct": CHANGE_THRESHOLD * 100,
            "verify_delay_s": VERIFY_DELAY_S,
        }
