"""LocalInference — abstract backend interface for on-device LLM inference.

HybridCoordinator holds a reference to LocalInference (the ABC), never to
a concrete implementation. This allows swapping backends without touching
routing logic.

Concrete implementations:
  OllamaInference    — Phase 1 dev backend  (~450 ms, easy setup)
  VLLMInference      — Phase 2 prod target  (~280 ms, task 2.13)
  NemotronInference  — NVIDIA Nemotron via Ollama (task 2.13 benchmark candidate)
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from command_executor import Command

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Action vocabulary prompt fragment (shared by all backends)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a desktop control assistant. Convert the user's natural-language \
request into exactly ONE action from the following vocabulary:

CLICK <target>       — click a named UI element or coordinates
SCROLL <direction> [<amount>]  — scroll up/down/left/right
TYPE <text>          — type literal text
OPEN <app-or-file>   — open an application or file
CLOSE [<target>]     — close the active or named window
HOTKEY <key1> [<key2>...]  — press a key combination
DICTATE <text>       — paste text verbatim via clipboard
CLARIFY <question>   — ask the user to clarify; do not act
SCREENSHOT           — capture the desktop screen

Rules:
- Reply with ONLY the action string, nothing else.
- Do not explain or comment.
- If the request is ambiguous reply with CLARIFY followed by a short question.
- If the request matches no action reply with CLARIFY.
"""


def _build_prompt(cmd: Command, few_shot_examples: list[dict] | None = None) -> str:
    """Build the full prompt sent to the LLM."""
    parts = [_SYSTEM_PROMPT]

    if few_shot_examples:
        parts.append("\nExamples:")
        for ex in few_shot_examples:
            parts.append(f'User: {ex["command_text"]}\nAssistant: {ex["action_text"]}')

    if cmd.session_context:
        context = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        parts.append(f"\nRecent commands:\n{context}")

    parts.append(f"\nUser: {cmd.text}\nAssistant:")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LocalInference(ABC):
    """Abstract LLM inference backend. All implementations are drop-in replacements."""

    @abstractmethod
    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        """Run inference and return an action string (e.g. 'CLICK Save button').

        Args:
            cmd: The command to classify.
            few_shot_examples: Optional list of {'command_text', 'action_text'} dicts
                from ContinuousTrainer; injected into the prompt when provided.
        """

    @abstractmethod
    def get_status(self) -> dict:
        """Return a status dict: {'backend': str, 'available': bool, ...}."""


# ---------------------------------------------------------------------------
# OllamaInference — Phase 1 dev backend
# ---------------------------------------------------------------------------

class OllamaInference(LocalInference):
    """Calls a local Ollama server via its HTTP API.

    Default model: llama3.1:8b  (4.6 GB VRAM — benchmarked 2026-05-13, 100% accuracy on
    all 12 test prompts covering 9 action verbs, robust on edge cases).

    Benchmark results on RTX 5090 (10 models, 12 prompts × 2 runs):
      llama3.1:8b      100% accuracy   4.6 GB   <- default
      llama3.2:3b      100% accuracy   6.3 GB
      qwen3-coder:30b  100% accuracy  18.1 GB   (code specialist)
      qwen2.5-coder     83% accuracy   0.9 GB
      nemotron-mini      25% accuracy   2.5 GB   (not suitable)
      gpt-oss:20b         0% accuracy   9.6 GB   (doesn't follow verb-first format)
      qwen3-vl:30b        0% accuracy  18.2 GB   (vision model, wrong task)

    Install: https://ollama.com  then: ollama pull llama3.1:8b
    """

    def __init__(
        self,
        model: str = "llama3.1:8b",
        host: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        self.model = model
        self.host = host
        self.timeout = timeout
        self._available: bool | None = None  # None = not yet checked

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        prompt = _build_prompt(cmd, few_shot_examples)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 64},
        }

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Ollama HTTP {resp.status}")
                    data = await resp.json()
                    action = data.get("response", "").strip().splitlines()[0].strip()
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info("OllamaInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                    self._available = True
                    return action
        except Exception as exc:
            self._available = False
            log.error("OllamaInference failed: %s", exc)
            return f"CLARIFY inference error: {exc}"

    def get_status(self) -> dict:
        return {
            "backend": "ollama",
            "model": self.model,
            "host": self.host,
            "available": self._available,
        }


# ---------------------------------------------------------------------------
# VLLMInference — Phase 2 production backend (stub)
# ---------------------------------------------------------------------------

class VLLMInference(LocalInference):
    """vLLM AsyncLLMEngine backend. ~280 ms on RTX 5090.

    Task 2.13: benchmark against OllamaInference and promote to default
    if p95 < 350 ms and setup is stable.

    Install: pip install vllm
    """

    def __init__(self, model: str = "meta-llama/Llama-3.1-70B-Instruct") -> None:
        self.model = model
        self._engine: Any = None

    def _load(self) -> None:
        if self._engine is not None:
            return
        try:
            from vllm import AsyncLLMEngine, AsyncEngineArgs
            args = AsyncEngineArgs(
                model=self.model,
                gpu_memory_utilization=0.85,
                max_model_len=4096,
                dtype="float16",
            )
            self._engine = AsyncLLMEngine.from_engine_args(args)
            log.info("VLLMInference: engine loaded for %s", self.model)
        except ImportError:
            raise RuntimeError("vllm not installed — run: pip install vllm")

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            self._load()
        except RuntimeError as exc:
            return f"CLARIFY {exc}"

        from vllm import SamplingParams
        prompt = _build_prompt(cmd, few_shot_examples)
        params = SamplingParams(temperature=0.0, max_tokens=64)

        t0 = time.monotonic()
        results = []
        async for output in self._engine.generate(prompt, params, request_id=str(id(cmd))):
            results = output.outputs
        latency_ms = (time.monotonic() - t0) * 1000
        action = results[0].text.strip().splitlines()[0].strip() if results else "CLARIFY no output"
        log.info("VLLMInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
        return action

    def get_status(self) -> dict:
        return {
            "backend": "vllm",
            "model": self.model,
            "available": self._engine is not None,
        }


# ---------------------------------------------------------------------------
# NemotronInference — NVIDIA Nemotron via Ollama (task 2.13 benchmark candidate)
# ---------------------------------------------------------------------------

class NemotronInference(OllamaInference):
    """Calls a local Ollama server with an NVIDIA Nemotron model.

    Nemotron models are available via Ollama and are optimised for RTX hardware
    by NVIDIA.  This backend is otherwise identical to OllamaInference —
    same HTTP API, same prompt format, same timeout handling.

    Recommended models (pull with `ollama pull <model>`):
      nemotron-mini          — 4B, ~8 GB VRAM, fastest
      nemotron               — 70B, ~40 GB VRAM (RAM-offload on 32 GB VRAM)

    Task 2.13: benchmark against OllamaInference (Llama 3.1 70B) and VLLMInference.
    Decision criterion: if Nemotron p95 < 350 ms and quality is acceptable, promote.
    """

    def __init__(
        self,
        model: str = "nemotron-mini",      # 4B default; swap to "nemotron" for 70B
        host: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        super().__init__(model=model, host=host, timeout=timeout)

    def get_status(self) -> dict:
        status = super().get_status()
        status["backend"] = "nemotron"
        return status
