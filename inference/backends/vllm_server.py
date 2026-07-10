from __future__ import annotations

import json
import logging
import time

from core.command_executor import Command

log = logging.getLogger(__name__)

from inference.backends.base import (
    LocalInference, set_inference_capture, _SYSTEM_PROMPT
)
from inference.backends.vllm import VLLMInference

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
        # Latched breaker (E7): a hung `vllm serve` otherwise costs the full
        # timeout on every request. Mirrors OllamaInference — fail fast after a
        # few failures, slow-call-aware so a degrading server trips early.
        from core.circuit_breaker import CircuitBreaker
        self._breaker = CircuitBreaker(
            name="vllm-server", fail_threshold=3, cooldown_s=30.0,
            slow_call_s=max(1.0, timeout * 0.8),
        )

    # ---------------------------------------------------------------------- #
    # Message construction — identical shape to VLLMInference.infer()
    # ---------------------------------------------------------------------- #

    def _build_messages(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None,
        counterexamples: list[dict] | None = None,
    ) -> list[dict]:
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
        return messages

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
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        if not self._breaker.allow():
            return "CLARIFY inference backend unavailable (circuit open)"
        _probe_gen = self._breaker.probe_gen   # tag this probe's outcome (#16)

        payload = {
            "model": self.model,
            "messages": self._build_messages(cmd, few_shot_examples, counterexamples),
            "temperature": 0.0,
            "max_tokens": 64,
            # vLLM's OpenAI server accepts guided_regex as an extra body field —
            # equivalent to StructuredOutputsParams(regex=...) on the in-process path.
            "guided_regex": self._VERB_PATTERN,
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
                    usage = data.get("usage") or {}
                    set_inference_capture(
                        prompt_json,
                        usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    )
                    latency_ms = (time.monotonic() - t0) * 1000
                    log.info("VLLMServerInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                    self._available = True
                    succeeded = True
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
        """Stream tokens via the vLLM server's OpenAI-compatible SSE stream."""
        try:
            import aiohttp
        except ImportError:
            yield "CLARIFY aiohttp not installed"
            return

        # guided_regex is intentionally omitted: infer_stream() is used only for
        # CLARIFY/EXPLAIN conversational responses (free-form text for TTS), not
        # for action classification.  Do not add it here without updating callers.
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
        """Return cached availability — does NOT make a blocking network call.

        Call ``await check_health()`` separately when a live probe is needed
        (e.g. startup table).  This keeps get_status() safe to call from any
        synchronous context without stalling the event loop.
        """
        return {
            "backend": "vllm-server",
            "model": self.model,
            "available": self._available,
            "server_url": self.base_url,
            "sleeping": False,  # server lifecycle is external — never sleeps via this class
        }

    async def check_health(self) -> bool:
        """Probe GET /v1/models and update the cached availability flag."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self.base_url}{self._MODELS_PATH}",
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as r:
                    self._available = r.status == 200
        except Exception:
            self._available = False
        return bool(self._available)

