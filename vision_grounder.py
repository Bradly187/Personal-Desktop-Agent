"""VisionGrounder — resolves named UI targets to pixel coordinates via Claude vision.

Called by HybridCoordinator after gate evaluation for CLICK verbs with a named
target. Eliminates the Tesseract word-match-only failure mode that causes ~58%
of voice CLICK commands to fall through to CLARIFY.

Fallback chain (enforced by HybridCoordinator, not here):
    1. Vision grounding (confidence ≥ GROUNDING_MIN_CONFIDENCE)
    2. Gaze coords from original Command
    3. Tesseract OCR word-match (find_text_on_screen)
    4. Current cursor position + CLARIFY

Cache: target_lower → (x, y, expiry_mono) — 2 s TTL per target name.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

GROUNDING_MIN_CONFIDENCE = 0.7
_CACHE_TTL_S = 2.0
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 64

_PROMPT_TEMPLATE = (
    'Find the UI element described as: "{target}"\n\n'
    "Respond with ONLY a JSON object (no markdown, no explanation):\n"
    '{"x": <pixel_x>, "y": <pixel_y>, "confidence": <0.0_to_1.0>}\n\n'
    "If the element is not visible, respond with:\n"
    '{"x": null, "y": null, "confidence": 0.0}'
)


@dataclass
class GroundingResult:
    x: int
    y: int
    confidence: float
    source: str = "vision"


class VisionGrounder:
    """Resolves a named UI target to pixel (x, y) using Claude vision."""

    def __init__(self, model: str = _MODEL) -> None:
        self._model = model
        self._client = None
        self._cache: dict[str, tuple[int, int, float]] = {}

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except ImportError:
                raise RuntimeError(
                    "anthropic package not installed — run: pip install anthropic"
                )
        return self._client

    def ground(
        self, target: str, screenshot_b64: str
    ) -> Optional[GroundingResult]:
        """Resolve a named UI element to pixel coords. Blocking — call via to_thread.

        Args:
            target: Human-readable element name, e.g. "submit button", "File menu".
            screenshot_b64: Base64-encoded PNG of the current desktop.

        Returns:
            GroundingResult or None if element not found / confidence too low.
        """
        if not target or not screenshot_b64:
            return None

        cache_key = target.lower().strip()
        now = time.monotonic()
        if cache_key in self._cache:
            cx, cy, expiry = self._cache[cache_key]
            if now < expiry:
                log.debug("VisionGrounder cache hit: %r → (%d, %d)", target, cx, cy)
                return GroundingResult(x=cx, y=cy, confidence=1.0, source="vision_cache")

        try:
            client = self._get_client()
        except RuntimeError as exc:
            log.warning("VisionGrounder unavailable: %s", exc)
            return None

        try:
            parsed = self._ask_claude(client, screenshot_b64, target)
        except Exception as exc:
            log.warning("VisionGrounder._ask_claude raised: %s", exc)
            return None

        if parsed is None:
            return None

        x, y, confidence = parsed
        if confidence < GROUNDING_MIN_CONFIDENCE:
            log.info(
                "VisionGrounder: low confidence %.2f for %r — falling through",
                confidence,
                target,
            )
            return None

        self._cache[cache_key] = (x, y, now + _CACHE_TTL_S)
        log.info("VisionGrounder: %r → (%d, %d) conf=%.2f", target, x, y, confidence)
        return GroundingResult(x=x, y=y, confidence=confidence)

    def _ask_claude(
        self, client, screenshot_b64: str, target: str
    ) -> Optional[tuple[int, int, float]]:
        """Send screenshot + target to Claude and parse the JSON response."""
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": screenshot_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": _PROMPT_TEMPLATE.format(target=target),
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:
            log.warning("VisionGrounder API call failed: %s", exc)
            return None

        raw = response.content[0].text.strip()
        # Strip markdown code fences if the model wraps its JSON response
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("VisionGrounder: unparseable response %r: %s", raw, exc)
            return None

        x = data.get("x")
        y = data.get("y")
        confidence = float(data.get("confidence", 0.0))

        if x is None or y is None:
            return None

        return int(x), int(y), confidence

    def get_status(self) -> dict:
        return {
            "model": self._model,
            "cache_entries": len(self._cache),
            "min_confidence": GROUNDING_MIN_CONFIDENCE,
            "client_ready": self._client is not None,
        }
