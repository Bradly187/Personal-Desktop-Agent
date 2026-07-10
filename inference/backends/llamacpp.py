from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, Optional

from core.command_executor import Command

log = logging.getLogger(__name__)

from inference.backends.base import (
    LocalInference, set_inference_capture, _SYSTEM_PROMPT
)

# ---------------------------------------------------------------------------
# LlamaCppInference — llama.cpp server backend (OpenAI-compatible API)
# ---------------------------------------------------------------------------

class LlamaCppInference(LocalInference):
    """Connects to a running llama-server (llama.cpp) via its OpenAI-compatible HTTP API.

    llama.cpp gives access to models that can be split across VRAM and RAM via
    --n-gpu-layers, enabling 27B–72B models alongside Whisper on the RTX 5090.

    Recommended model: Qwen3.6-27B-Q4_K_M (17 GB VRAM, 68.9% SWE-Bench Verified,
    ~158 tok/s on RTX 5090, fully in VRAM at Q4_K_M).

    Server launch (run in a separate terminal):
        llama-server \\
            --model /path/to/Qwen3.6-27B-Q4_K_M.gguf \\
            --n-gpu-layers 999 \\
            --ctx-size 16384 \\
            --port 8080

    Activate via:
        python main.py --backend llamacpp

    See docs/llama_server_setup.md for full setup instructions.
    """

    _API_PATH = "/v1/chat/completions"

    def __init__(
        self,
        model: str = "local-model",    # name shown in logs; server ignores it
        host: str = "http://localhost:8080",
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._available: bool | None = None
        # Latched breaker (E7): a hung llama-server otherwise costs the full
        # timeout on every request. Mirrors OllamaInference.
        from core.circuit_breaker import CircuitBreaker
        self._breaker = CircuitBreaker(
            name="llamacpp", fail_threshold=3, cooldown_s=30.0,
            slow_call_s=max(1.0, timeout * 0.8),
        )

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
        counterexamples: list[dict] | None = None,
    ) -> str:
        try:
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        if not self._breaker.allow():
            return "CLARIFY inference backend unavailable (circuit open)"
        _probe_gen = self._breaker.probe_gen   # tag this probe's outcome (#16)

        # Build OpenAI-compatible chat messages from the shared prompt builder
        system_prompt = _SYSTEM_PROMPT
        if counterexamples:
            neg_block = "Avoid these incorrect mappings:\n" + "\n".join(
                f'"{ex["command_text"]}" -> NOT "{ex["wrong_action"]}"'
                for ex in counterexamples
            )
            system_prompt = system_prompt + "\n\n" + neg_block
        user_content = _build_user_content(cmd, few_shot_examples)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": 64,
        }

        prompt_json = json.dumps(payload["messages"])
        set_inference_capture(prompt_json)
        t0 = time.monotonic()
        # Report the breaker outcome in `finally` so a CancelledError still
        # clears the half-open probe flag (mirrors OllamaInference).
        succeeded = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}{self._API_PATH}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"llama-server HTTP {resp.status}")
                    data = await resp.json()
                    action = (
                        data["choices"][0]["message"]["content"]
                        .strip()
                        .splitlines()[0]
                        .strip()
                    )
                    usage = data.get("usage") or {}
                    set_inference_capture(
                        prompt_json,
                        usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    )
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info("LlamaCppInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                    self._available = True
                    succeeded = True
                    return action
        except Exception as exc:
            self._available = False
            # Raw transport error stays in the log; user-facing CLARIFY is stable.
            log.error("LlamaCppInference failed: %s", exc)
            return "CLARIFY the local model is unavailable right now. Please try again."
        finally:
            if succeeded:
                self._breaker.record_success(_probe_gen, latency_s=time.monotonic() - t0)
            else:
                self._breaker.record_failure(_probe_gen)

    async def infer_stream(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ):
        """Stream tokens via llama-server's OpenAI-compatible SSE stream."""
        try:
            import aiohttp
        except ImportError:
            yield "CLARIFY aiohttp not installed"
            return

        system_prompt = _SYSTEM_PROMPT
        user_content = _build_user_content(cmd, few_shot_examples)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": True,
        }

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}{self._API_PATH}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60.0),
                ) as resp:
                    if resp.status != 200:
                        yield f"CLARIFY llama-server HTTP {resp.status}"
                        return
                    async for raw_line in resp.content:
                        line = raw_line.decode().strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            try:
                                chunk = __import__("json").loads(line[6:])
                                token = (
                                    chunk.get("choices", [{}])[0]
                                    .get("delta", {})
                                    .get("content", "")
                                )
                                if token:
                                    yield token
                            except Exception:
                                continue
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("LlamaCppInference.stream: %r complete (%.0f ms)", cmd.text[:40], latency_ms)
            self._available = True
        except Exception as exc:
            self._available = False
            log.error("LlamaCppInference.stream failed: %s", exc)
            yield f"CLARIFY inference error: {exc}"

    def get_status(self) -> dict:
        return {
            "backend": "llamacpp",
            "model": self.model,
            "host": self.host,
            "available": self._available,
        }


def _build_user_content(cmd: Command, few_shot_examples: list[dict] | None) -> str:
    """Build the user message content (system prompt is passed separately for chat models)."""
    parts: list[str] = []

    if few_shot_examples:
        parts.append("Examples:")
        for ex in few_shot_examples:
            parts.append(f'User: {ex["command_text"]}\nAssistant: {ex["action_text"]}')

    if cmd.session_context:
        context = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        parts.append(f"Recent commands:\n{context}")

    parts.append(f"User: {cmd.text}")
    return "\n".join(parts) if parts else cmd.text
