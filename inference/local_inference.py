"""LocalInference — abstract backend interface for on-device LLM inference.

HybridCoordinator holds a reference to LocalInference (the ABC), never to
a concrete implementation. This allows swapping backends without touching
routing logic.

Concrete implementations:
  OllamaInference    — default backend  (373 ms warm p50, 100% accuracy on command eval)
  VLLMInference      — production backend using vLLM offline LLM class (not AsyncLLMEngine);
                       uses LLM.chat() for native chat templates + sleep/wake VRAM offload
  LlamaCppInference  — llama-server HTTP backend (--backend llamacpp)

Embedding:
  VLLMEmbedder       — vLLM LLM.encode() for semantic memory RAG; replaces sentence-transformers
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from core.command_executor import Command

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

    async def infer_stream(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ):
        """Stream inference tokens as they arrive (AsyncIterator[str]).

        Default implementation buffers the full response and yields it as a
        single token — safe for backends that don't support streaming.
        Override in subclasses (e.g. OllamaInference) for true token-by-token.

        Used by TTS paths (CLARIFY questions, DevAgent EXPLAIN responses) where
        starting audio synthesis before the full response is ready reduces latency.
        """
        result = await self.infer(cmd, few_shot_examples)
        yield result

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

    async def infer_stream(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ):
        """Stream tokens from Ollama as they arrive (true token-by-token).

        Uses Ollama's native streaming API (stream=True) so each token is
        yielded as soon as it's generated. Used by TTS paths (CLARIFY, EXPLAIN)
        to start audio synthesis before the full response is ready.

        num_predict is raised to 512 for conversational responses vs the 64
        used in the command-classification path (infer).
        """
        try:
            import aiohttp
        except ImportError:
            yield "CLARIFY aiohttp not installed"
            return

        prompt = _build_prompt(cmd, few_shot_examples)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,                                 # token-by-token
            "options": {"temperature": 0.0, "num_predict": 512},
        }

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.host}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60.0),
                ) as resp:
                    if resp.status != 200:
                        yield f"CLARIFY Ollama HTTP {resp.status}"
                        return
                    async for raw_line in resp.content:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            chunk = __import__("json").loads(line)
                        except Exception:
                            continue
                        token = chunk.get("response", "")
                        if token:
                            yield token
                        if chunk.get("done"):
                            latency_ms = (time.monotonic() - t0) * 1000
                            log.info(
                                "OllamaInference.stream: %r complete (%.0f ms)",
                                cmd.text[:40], latency_ms,
                            )
                            self._available = True
                            return
        except Exception as exc:
            self._available = False
            log.error("OllamaInference.stream failed: %s", exc)
            yield f"CLARIFY inference error: {exc}"

    def get_status(self) -> dict:
        return {
            "backend": "ollama",
            "model": self.model,
            "host": self.host,
            "available": self._available,
        }


# ---------------------------------------------------------------------------
# VLLMInference — production backend using vLLM offline LLM class
# ---------------------------------------------------------------------------

class VLLMInference(LocalInference):
    """vLLM offline LLM class backend for the command domain (Gemma 4 E4B-IT).

    Uses LLM.chat() with grammar-constrained decoding to guarantee 100% format
    accuracy on the 16-verb action vocabulary regardless of the model's tendency
    to add preambles or explanation.

    Model: google/gemma-4-E4B-it  (~4.5B effective params, dense)
      FP16: ~9-10 GB — too large without quantization
      bitsandbytes INT4: ~2.5 GB  ← default (no special HF checkpoint required)

    VRAM budget (32 GB RTX 5090):
      Baseline + Whisper:         ~12.5 GB
      E4B-IT compressed-tensors:   ~11.2 GB  (gpu_util=0.35; weights ~5-6 GB + KV cache)
      Remaining when command awake: ~8.3 GB  — too little for 31B specialist;
                                               pool sleeps command before waking 31B.
      Sleep latency for E4B-IT:    ~0.2s  (5-6 GB over PCIe 5.0 — still negligible)

    Grammar constraints:
      GuidedDecodingParams(regex=_VERB_PATTERN) forces every output to begin
      with one of the 16 valid action verbs.  Eliminates format failures without
      any accuracy penalty on well-formed requests.

    Default checkpoint: cyankiwi/gemma-4-E4B-it-AWQ-INT4
      - 8B params, compressed-tensors INT4 (Neural Magic format)
      - Ungated, Apache 2.0, no HF login required; 204k downloads
      - quantization=None → vLLM auto-detects "compressed-tensors" from model config
        (specifying quantization="awq" will FAIL — mismatch error)

    Fallback options:
      model="unsloth/gemma-4-E4B-it-unsloth-bnb-4bit", quantization="bitsandbytes"
      model="google/gemma-4-E4B-it",                    quantization="bitsandbytes"

    Install (WSL2, ~/.venv-wsl):
        pip install vllm
        hf download cyankiwi/gemma-4-E4B-it-AWQ-INT4
    """

    # 11 accessibility verbs + CLARIFY — the only verbs VLLMInference emits.
    # Dev-agent verbs (WRITE_FILE, RUN_TERMINAL, EXPLAIN, SEARCH_WEB, READ_SCREEN)
    # are routed by DomainClassifier → DevAgent → ModelRouter before they reach here.
    _VERB_PATTERN: str = (
        r"(CLICK|MOUSEDOWN|MOUSEUP|SCROLL|TYPE|OPEN|CLOSE|HOTKEY"
        r"|DICTATE|CLARIFY|SCREENSHOT)( .*)?"
    )

    _GPU_UTIL: float = 0.50        # 16 GB of 32 GB — measured model overhead is ~12 GB
    # Breakdown (measured 2026-05-30, vLLM 0.21.0, compressed-tensors 8B):
    #   model weights:   10.08 GB  (compressed-tensors includes large metadata overhead)
    #   CUDA graphs:      0.45 GB  (bypassed via enforce_eager=True — see _blocking_load)
    #   PyTorch overhead: ~1.5 GB
    #   KV cache:        ~4.0 GB   (16 - 10.08 - 1.5 = ~4.4 GB)
    # Safe because sleep/wake: command model sleeps before specialist wakes, and
    # only the command model OR a specialist is ever awake at a time.
    _MAX_MODEL_LEN: int = 4096
    _INFER_TIMEOUT_S: float = 15.0

    def __init__(
        self,
        model: str = "cyankiwi/gemma-4-E4B-it-AWQ-INT4",
        gpu_memory_utilization: float | None = None,
        quantization: str | None = None,   # None = auto-detect from model config
        speculative_model: str | None = None,
    ) -> None:
        self.model = model
        self._gpu_util = gpu_memory_utilization if gpu_memory_utilization is not None else self._GPU_UTIL
        self._quantization = quantization
        self._speculative_model = speculative_model
        self._llm: Any = None
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()
        self._sleeping: bool = False
        # Optional async hook invoked just before this engine occupies the GPU
        # (load or wake).  The specialist pool sets it to sleep any awake
        # specialist first, enforcing mutual exclusion: command XOR specialist
        # resident at any time (alongside Whisper).
        self._pre_wake_hook: Any = None

    def set_pre_wake_hook(self, hook: Any) -> None:
        """Register an async callable run before the command engine wakes/loads.

        Used by VLLMSpecialistPool to sleep an active specialist so the command
        model can reclaim the GPU.  Pass None to clear.
        """
        self._pre_wake_hook = hook

    # ---------------------------------------------------------------------- #
    # Engine lifecycle
    # ---------------------------------------------------------------------- #

    async def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        async with self._load_lock:
            if self._llm is not None:
                return
            # About to occupy the GPU — let the pool unload any active specialist
            # first so we don't OOM (command + 26B + Whisper exceed 32 GB).
            if self._pre_wake_hook is not None:
                try:
                    await self._pre_wake_hook()
                except Exception as _hook_exc:  # never block command on hook failure
                    log.warning("VLLMInference: pre-wake hook failed: %s", _hook_exc)
            t0 = time.monotonic()
            try:
                self._llm = await asyncio.to_thread(self._blocking_load)
                self._load_error = None
                self._sleeping = False
                log.info("VLLMInference: engine ready — %s (%.1fs)",
                         self.model, time.monotonic() - t0)
            except Exception as exc:
                self._load_error = str(exc)
                log.error("VLLMInference: load failed — %s", exc)
                raise

    def _blocking_load(self) -> Any:
        try:
            from vllm import LLM
        except ImportError as _exc:
            raise RuntimeError(
                f"vllm import failed: {_exc}\n"
                "  Verify: source ~/.venv-wsl/bin/activate && python -c 'from vllm import LLM'"
            ) from _exc
        kwargs: dict = dict(
            model=self.model,
            gpu_memory_utilization=self._gpu_util,
            max_model_len=self._MAX_MODEL_LEN,
            dtype="auto",
            trust_remote_code=False,
            # Skip CUDA graph compilation (saves 0.45 GB VRAM + ~2 min cold-start
            # compile time).  For the command domain (max 64 output tokens, single
            # user) eager execution adds <5ms per request — negligible.
            enforce_eager=True,
            # NO enable_sleep_mode: the command engine is fully torn down (not
            # vLLM-slept) to free VRAM for a specialist, so it never needs the
            # CuMem allocator. Keeping it off avoids the Blackwell CuMem conflict
            # between two concurrent sleep-mode engines. See VLLMInference.sleep().
        )
        if self._quantization:
            kwargs["quantization"] = self._quantization
            if self._quantization == "bitsandbytes":
                # Pre-quantized BnB checkpoint (e.g. unsloth bnb-4bit) needs this;
                # runtime BnB quantization of a BF16 model also uses this path.
                kwargs["load_format"] = "bitsandbytes"
            # AWQ: no extra load_format needed — vLLM detects it from the checkpoint config.
        if self._speculative_model:
            kwargs["speculative_model"] = self._speculative_model
            kwargs["num_speculative_tokens"] = 5
            log.info("VLLMInference: speculative decoding enabled  draft=%s",
                     self._speculative_model)
        return LLM(**kwargs)

    async def sleep(self) -> None:
        """Fully unload the engine to free VRAM (~16-19 GB), then reload on demand.

        We DESTROY rather than vLLM-sleep: two concurrent enable_sleep_mode
        (CuMem) engines conflict on Blackwell ("CUDA Error: device not ready at
        cumem_allocator.cpp"). Full teardown releases the CuMem-free command
        engine cleanly so the specialist (the lone CuMem engine) can allocate.
        Cost: the command engine reloads (~50s cold, faster warm) on next infer().
        """
        async with self._load_lock:
            if self._llm is not None:
                await asyncio.to_thread(self._teardown)
                log.info("VLLMInference: unloaded (%s) — VRAM freed", self.model)

    def _teardown(self) -> None:
        """Blocking: release the LLM engine + EngineCore subprocess and free VRAM."""
        llm, self._llm = self._llm, None
        self._sleeping = False
        # Best-effort explicit engine shutdown before dropping the reference.
        try:
            shutdown = getattr(getattr(llm, "llm_engine", None), "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception as exc:
            log.debug("VLLMInference: engine.shutdown() noop/err: %s", exc)
        del llm
        try:
            import gc
            gc.collect()
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    async def wake_up(self) -> None:
        """Reload the engine from scratch (full re-init)."""
        await self._ensure_loaded()

    # ---------------------------------------------------------------------- #
    # Inference
    # ---------------------------------------------------------------------- #

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            await self._ensure_loaded()
        except Exception as exc:
            return f"CLARIFY vllm unavailable: {exc}"

        try:
            from vllm import SamplingParams
        except ImportError:
            return "CLARIFY vllm not installed"

        # Grammar-constrained decoding: force first token to be a valid verb.
        # API changed across vLLM versions — try each in order:
        #   vLLM 0.21.x: SamplingParams(structured_outputs=StructuredOutputsParams(regex=...))
        #   vLLM 0.6.x:  SamplingParams(guided_decoding=GuidedDecodingParams(regex=...))
        #   fallback:    SamplingParams(stop=["\n"])  — no constraint, format-only
        _constraint_kwargs: dict = {}
        try:
            from vllm.sampling_params import StructuredOutputsParams
            _constraint_kwargs = {
                "structured_outputs": StructuredOutputsParams(regex=self._VERB_PATTERN)
            }
        except ImportError:
            try:
                from vllm.sampling_params import GuidedDecodingParams
                _constraint_kwargs = {
                    "guided_decoding": GuidedDecodingParams(regex=self._VERB_PATTERN)
                }
            except ImportError:
                _constraint_kwargs = {"stop": ["\n"]}
                log.debug("VLLMInference: no structured-output API found — using stop=[\\n]")

        # Build chat messages — LLM.chat() applies the model's native template.
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if few_shot_examples:
            for ex in few_shot_examples:
                messages.append({"role": "user",      "content": ex["command_text"]})
                messages.append({"role": "assistant", "content": ex["action_text"]})
        if cmd.session_context:
            ctx = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
            messages.append({"role": "user",      "content": f"Recent commands:\n{ctx}"})
            messages.append({"role": "assistant", "content": "Understood."})
        messages.append({"role": "user", "content": cmd.text})

        sampling_kwargs: dict = dict(temperature=0.0, max_tokens=64)
        sampling_kwargs.update(_constraint_kwargs)
        sampling = SamplingParams(**sampling_kwargs)

        t0 = time.monotonic()
        try:
            outputs = await asyncio.to_thread(
                self._llm.chat,
                [messages],
                sampling_params=sampling,
                use_tqdm=False,
            )
        except Exception as exc:
            log.error("VLLMInference.chat failed: %s", exc)
            return f"CLARIFY inference error: {exc}"

        latency_ms = (time.monotonic() - t0) * 1000

        if not outputs or not outputs[0].outputs:
            return "CLARIFY no output from vllm"

        action = outputs[0].outputs[0].text.strip().splitlines()[0].strip()
        constrained = "structured_outputs" in _constraint_kwargs or "guided_decoding" in _constraint_kwargs
        log.info("VLLMInference: %r → %r (%.0f ms)%s",
                 cmd.text, action, latency_ms,
                 "" if constrained else " [unconstrained]")
        return action

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "backend": "vllm",
            "model": self.model,
            "quantization": self._quantization,
            "available": self._llm is not None,
            "sleeping": self._sleeping,
            "load_error": self._load_error,
            "gpu_memory_utilization": self._gpu_util,
            "speculative_model": self._speculative_model,
        }


# ---------------------------------------------------------------------------
# VLLMServerInference — HTTP client for a `vllm serve` OpenAI-compatible server
# ---------------------------------------------------------------------------

class VLLMServerInference(LocalInference):
    """Talks to a `vllm serve` OpenAI-compatible server over HTTP.

    This is the Windows-friendly alternative to the in-process VLLMInference:
    vLLM (with its `vllm._C` CUDA extension) only builds cleanly on Linux, so we
    run `vllm serve <model>` inside WSL2 and reach it from the Windows side over
    localhost. WSL2 forwards the server's 0.0.0.0:8000 to Windows localhost:8000,
    so no special networking is required.

    The server lifecycle is managed EXTERNALLY (see scripts/start_vllm_server.sh):
    this class never loads or unloads a model — wake_up()/sleep() are no-ops.

    Start the server (inside WSL2):
        wsl bash scripts/start_vllm_server.sh
        # or double-click scripts/start_vllm_server.bat on Windows

    Activate via:
        python main.py --backend vllm-server [--vllm-server-url http://localhost:8000]

    Modelled on LlamaCppInference (same aiohttp session pattern, SSE parsing,
    OpenAI-compatible /v1/chat/completions endpoint) but adds vLLM's
    `guided_regex` grammar constraint to force valid action-verb output, exactly
    like the in-process VLLMInference does with StructuredOutputsParams.
    """

    _CHAT_PATH = "/v1/chat/completions"
    _MODELS_PATH = "/v1/models"

    # Same grammar constraint as the in-process backend — force the first token
    # to be one of the 11 accessibility verbs (or CLARIFY).
    _VERB_PATTERN: str = VLLMInference._VERB_PATTERN

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "meta-llama/Meta-Llama-3.1-8B-Instruct",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._available: bool | None = None  # None = not yet checked

    # ---------------------------------------------------------------------- #
    # Message construction — identical shape to VLLMInference.infer()
    # ---------------------------------------------------------------------- #

    def _build_messages(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None,
    ) -> list[dict]:
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if few_shot_examples:
            for ex in few_shot_examples:
                messages.append({"role": "user",      "content": ex["command_text"]})
                messages.append({"role": "assistant", "content": ex["action_text"]})
        if cmd.session_context:
            ctx = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
            messages.append({"role": "user",      "content": f"Recent commands:\n{ctx}"})
            messages.append({"role": "assistant", "content": "Understood."})
        messages.append({"role": "user", "content": cmd.text})
        return messages

    # ---------------------------------------------------------------------- #
    # Inference
    # ---------------------------------------------------------------------- #

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        payload = {
            "model": self.model,
            "messages": self._build_messages(cmd, few_shot_examples),
            "temperature": 0.0,
            "max_tokens": 64,
            # vLLM's OpenAI server accepts guided_regex as an extra body field —
            # equivalent to StructuredOutputsParams(regex=...) on the in-process path.
            "guided_regex": self._VERB_PATTERN,
        }

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}{self._CHAT_PATH}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        self._available = False
                        log.error("VLLMServerInference HTTP %s: %s", resp.status, body[:200])
                        return f"CLARIFY vLLM server error: {resp.status} {body[:200]}"
                    data = __import__("json").loads(body)
                    content = data["choices"][0]["message"]["content"].strip()
                    action = content.splitlines()[0].strip() if content else "CLARIFY empty response"
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info("VLLMServerInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                    self._available = True
                    return action
        except aiohttp.ClientConnectorError as exc:
            self._available = False
            log.error("VLLMServerInference: unreachable at %s: %s", self.base_url, exc)
            return (
                f"CLARIFY vLLM server unreachable at {self.base_url} — "
                f"run: wsl vllm serve {self.model}"
            )
        except Exception as exc:
            self._available = False
            log.error("VLLMServerInference failed: %s", exc)
            return f"CLARIFY vLLM server error: {exc}"

    async def infer_stream(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ):
        """Stream tokens via the vLLM server's OpenAI-compatible SSE stream."""
        try:
            import aiohttp
        except ImportError:
            yield "CLARIFY aiohttp not installed"
            return

        payload = {
            "model": self.model,
            "messages": self._build_messages(cmd, few_shot_examples),
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": True,
        }

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}{self._CHAT_PATH}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60.0),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield f"CLARIFY vLLM server error: {resp.status} {body[:200]}"
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
            log.info("VLLMServerInference.stream: %r complete (%.0f ms)", cmd.text[:40], latency_ms)
            self._available = True
        except aiohttp.ClientConnectorError as exc:
            self._available = False
            log.error("VLLMServerInference.stream: unreachable at %s: %s", self.base_url, exc)
            yield (
                f"CLARIFY vLLM server unreachable at {self.base_url} — "
                f"run: wsl vllm serve {self.model}"
            )
        except Exception as exc:
            self._available = False
            log.error("VLLMServerInference.stream failed: %s", exc)
            yield f"CLARIFY vLLM server error: {exc}"

    # ---------------------------------------------------------------------- #
    # External-lifecycle no-ops (server is managed by start_vllm_server.sh)
    # ---------------------------------------------------------------------- #

    async def wake_up(self) -> None:
        log.info("VLLMServerInference: wake_up() is a no-op — server lifecycle is "
                 "external (%s)", self.base_url)

    async def sleep(self) -> None:
        log.info("VLLMServerInference: sleep() is a no-op — server lifecycle is "
                 "external (%s)", self.base_url)

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        """Health-check the server via GET /v1/models (best-effort, short timeout)."""
        available = False
        try:
            import urllib.request as _ur
            with _ur.urlopen(f"{self.base_url}{self._MODELS_PATH}", timeout=2) as r:
                r.read()
            available = True
        except Exception:
            available = False
        self._available = available
        return {
            "backend": "vllm-server",
            "model": self.model,
            "available": available,
            "server_url": self.base_url,
            "sleeping": False,  # server lifecycle is external — never sleeps via this class
        }


# NemotronInference removed: nemotron-mini scored 25% on command eval (2026-05-13).


# ---------------------------------------------------------------------------
# VLLMEmbedder — vLLM LLM.encode() for semantic memory / codebase RAG
# ---------------------------------------------------------------------------

class VLLMEmbedder:
    """In-process embedding via vLLM's pooling API (LLM.encode).

    Replaces sentence-transformers (all-MiniLM-L6-v2) in SemanticMemory and
    CodebaseIndexer. Uses a dedicated small embedding model that stays resident
    at ~0.5–1 GB VRAM — negligible overhead alongside the specialist pool.

    Recommended models (HuggingFace):
        nomic-ai/nomic-embed-text-v1.5   — 137M params, 768-dim, best retrieval/size
        BAAI/bge-m3                      — 570M params, 1024-dim, best absolute quality
        Qwen/Qwen3-Embedding-0.6B        — 600M params, 1024-dim, multilingual

    The encoder is created lazily on first encode() call. It stays loaded
    permanently — embedding requests are cheap and frequent.

    Usage:
        embedder = VLLMEmbedder()
        vecs = await embedder.encode(["click the save button", "open terminal"])
        # vecs: list of numpy arrays, shape (dim,)
    """

    _GPU_UTIL: float = 0.05   # ~1.6 GB for a 0.5-1B embedding model on 32 GB

    def __init__(
        self,
        model: str = "nomic-ai/nomic-embed-text-v1.5",
        gpu_memory_utilization: float | None = None,
    ) -> None:
        self.model = model
        self._gpu_util = gpu_memory_utilization if gpu_memory_utilization is not None else self._GPU_UTIL
        self._llm: Any = None
        self._load_lock = asyncio.Lock()
        self._dim: int | None = None

    async def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        async with self._load_lock:
            if self._llm is not None:
                return
            self._llm = await asyncio.to_thread(self._blocking_load)
            log.info("VLLMEmbedder: ready — %s", self.model)

    def _blocking_load(self) -> Any:
        try:
            from vllm import LLM
        except ImportError:
            raise RuntimeError("vllm not installed")
        return LLM(
            model=self.model,
            task="embed",
            gpu_memory_utilization=self._gpu_util,
            dtype="auto",
            trust_remote_code=True,
        )

    async def encode(self, texts: list[str]) -> list[Any]:
        """Return a list of embedding vectors (numpy arrays), one per text."""
        await self._ensure_loaded()
        outputs = await asyncio.to_thread(
            self._llm.encode,
            texts,
            use_tqdm=False,
        )
        vecs = [o.outputs.embedding for o in outputs]
        if self._dim is None and vecs:
            self._dim = len(vecs[0])
            log.info("VLLMEmbedder: dim=%d", self._dim)
        return vecs

    def get_status(self) -> dict:
        return {
            "backend": "vllm_embed",
            "model": self.model,
            "available": self._llm is not None,
            "dim": self._dim,
        }


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

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        # Build OpenAI-compatible chat messages from the shared prompt builder
        system_prompt = _SYSTEM_PROMPT
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

        t0 = time.monotonic()
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
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info("LlamaCppInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                    self._available = True
                    return action
        except Exception as exc:
            self._available = False
            log.error("LlamaCppInference failed: %s", exc)
            return f"CLARIFY inference error: {exc}"

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
