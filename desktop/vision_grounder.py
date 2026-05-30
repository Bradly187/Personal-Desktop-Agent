"""VisionGrounder — resolves named UI targets to pixel coordinates via vision LLM.

Called by HybridCoordinator after gate evaluation for CLICK verbs with a named
target. Eliminates the Tesseract word-match-only failure mode that causes ~58%
of voice CLICK commands to fall through to CLARIFY.

Fallback chain (enforced by HybridCoordinator, not here):
    1. Vision grounding (confidence ≥ GROUNDING_MIN_CONFIDENCE)
    2. Gaze coords from original Command
    3. Tesseract OCR word-match (find_text_on_screen)
    4. Current cursor position + CLARIFY

Backends:
    "anthropic"  — claude-sonnet-4-6 via Anthropic API (~$0.003–0.015/call).
                   Original default. Use when local GPU is under load.
    "ollama"     — qwen3-vl:30b via Ollama at localhost:11434. Free, ~0.4s
                   warm latency on RTX 5090. Default when Ollama is reachable.

Cache: target_lower → (x, y, expiry_mono) — 2 s TTL per target name.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

GROUNDING_MIN_CONFIDENCE = 0.7
_CACHE_TTL_S = 2.0
_MODEL = "claude-sonnet-4-6"
_LOCAL_MODEL = "qwen3-vl:30b"
_LOCAL_URL = "http://localhost:11434/v1/chat/completions"
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
    """Resolves a named UI target to pixel (x, y) using a vision LLM.

    Args:
        model:   Anthropic model name (used when backend="anthropic").
        backend: "ollama" (default, free, qwen3-vl:30b on localhost) or
                 "anthropic" (claude-sonnet-4-6, ~$0.003–0.015/call).
    """

    def __init__(
        self,
        model: str = _MODEL,
        backend: str = "ollama",
    ) -> None:
        self._model = model
        self._backend = backend
        self._client = None  # Anthropic client, lazy-init
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

        # Sanitize: keep only printable ASCII to prevent prompt injection
        target = re.sub(r"[^\x20-\x7E]", "", target)[:128].strip()
        if not target:
            return None

        cache_key = target.lower().strip()
        now = time.monotonic()
        if cache_key in self._cache:
            cx, cy, expiry = self._cache[cache_key]
            if now < expiry:
                log.debug("VisionGrounder cache hit: %r → (%d, %d)", target, cx, cy)
                return GroundingResult(x=cx, y=cy, confidence=1.0, source="vision_cache")

        if self._backend == "ollama":
            try:
                parsed = self._ask_ollama(screenshot_b64, target)
            except Exception as exc:
                log.warning(
                    "VisionGrounder Ollama failed (%s), trying Anthropic fallback", exc
                )
                parsed = None
            if parsed is None:
                # Graceful fallback to Anthropic when local model is unavailable
                try:
                    client = self._get_client()
                    parsed = self._ask_claude(client, screenshot_b64, target)
                except Exception as exc2:
                    log.warning("VisionGrounder Anthropic fallback also failed: %s", exc2)
                    return None
        else:
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

    def _ask_ollama(
        self, screenshot_b64: str, target: str
    ) -> Optional[tuple[int, int, float]]:
        """Send screenshot + target to qwen3-vl:30b via Ollama OpenAI-compat API.

        Uses urllib (stdlib) so no extra dependency beyond what's already present.
        Raises on connection error — caller handles fallback to Anthropic.
        """
        payload = json.dumps({
            "model": _LOCAL_MODEL,
            "max_tokens": _MAX_TOKENS,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_b64}"
                            },
                        },
                        {
                            "type": "text",
                            "text": _PROMPT_TEMPLATE.format(target=target),
                        },
                    ],
                }
            ],
        }).encode()

        req = urllib.request.Request(
            _LOCAL_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())

        raw = body["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if the model wraps its response
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("VisionGrounder (Ollama): unparseable response %r: %s", raw, exc)
            return None

        x = data.get("x")
        y = data.get("y")
        confidence = float(data.get("confidence", 0.0))
        if x is None or y is None:
            return None
        return int(x), int(y), confidence

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
            "model": _LOCAL_MODEL if self._backend == "ollama" else self._model,
            "backend": self._backend,
            "cache_entries": len(self._cache),
            "min_confidence": GROUNDING_MIN_CONFIDENCE,
            "client_ready": self._client is not None or self._backend == "ollama",
        }
