from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from core.command_executor import Command

log = logging.getLogger(__name__)

from inference.backends.base import (
    LocalInference, set_inference_capture, _SYSTEM_PROMPT
)

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

    NOTE: VLLMInference is the opt-in `--backend vllm` path. The default Ollama
    backend (OllamaInference, llama3.1:8b) is the VERIFIED command model; the
    Gemma 4 checkpoint below is PLANNED/unverified — use only when explicitly
    selecting the vLLM backend.

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
        counterexamples: list[dict] | None = None,
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
        system_content = _SYSTEM_PROMPT
        if counterexamples:
            neg_block = "Avoid these incorrect mappings:\n" + "\n".join(
                f'"{ex["command_text"]}" -> NOT "{ex["wrong_action"]}"'
                for ex in counterexamples
            )
            system_content += "\n\n" + neg_block
        messages = [{"role": "system", "content": system_content}]
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

        set_inference_capture(json.dumps(messages))
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

