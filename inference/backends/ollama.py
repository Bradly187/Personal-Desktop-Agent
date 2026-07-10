from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

from core.command_executor import Command

log = logging.getLogger(__name__)

from inference.backends.base import (
    LocalInference, set_inference_capture,
)

# ---------------------------------------------------------------------------
# Local Ollama bootstrap — ensure the server is up before inference starts
# ---------------------------------------------------------------------------

_OLLAMA_DEFAULT_HOST = "http://localhost:11434"


def _ollama_alive(host: str = _OLLAMA_DEFAULT_HOST, timeout: float = 2.0) -> bool:
    """True if an Ollama server answers /api/version at `host`."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{host}/api/version", timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _find_ollama_exe() -> Optional[str]:
    """Locate the ollama executable: PATH first, then the standard install dirs."""
    import os
    import shutil
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles%\Ollama\ollama.exe"),
        "/usr/local/bin/ollama",
        "/usr/bin/ollama",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def ensure_ollama_running(host: str = _OLLAMA_DEFAULT_HOST, wait_s: float = 25.0) -> bool:
    """Best-effort: make sure a local Ollama server is listening, starting one if not.

    The command model (default backend) and the ModelRouter specialists both run
    on Ollama, so the agent depends on a live server. This starts ``ollama serve``
    **detached** when the port is dead so the server outlives agent restarts (the
    process group is independent ΓÇö restarting the agent won't kill it and evict
    resident models). Idempotent: a no-op when Ollama already answers.

    Returns True if Ollama is reachable afterwards. Degrades gracefully ΓÇö logs a
    warning and returns False if Ollama isn't installed or won't come up, leaving
    the agent to fall back to the cloud path rather than crashing.
    """
    if _ollama_alive(host):
        log.info("Ollama already running at %s", host)
        return True

    exe = _find_ollama_exe()
    if not exe:
        log.warning(
            "Ollama not found on PATH or in the default install location ΓÇö local "
            "inference unavailable; the agent will rely on cloud fallback. "
            "Install from https://ollama.com"
        )
        return False

    import subprocess
    import sys
    try:
        kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP ΓÇö independent of the agent.
            kwargs["creationflags"] = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kwargs)
        log.info("Ollama not running ΓÇö launched 'ollama serve' (%s)", exe)
    except Exception as exc:
        log.warning("Failed to start Ollama (%s) ΓÇö local inference may be unavailable", exc)
        return False

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _ollama_alive(host):
            log.info("Ollama is up at %s", host)
            return True
        time.sleep(0.5)
    log.warning("Ollama did not become ready within %.0fs at %s", wait_s, host)
    return False


# ---------------------------------------------------------------------------
# Action vocabulary prompt fragment (shared by all backends)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a desktop control assistant. Convert the user's natural-language \
request into exactly ONE action from the following vocabulary. The angle \
brackets below mark placeholders ΓÇö replace each with the actual value. Never \
output the brackets, the placeholder name, surrounding quotes, or an '=' sign.

CLICK <target>       ΓÇö click a named UI element or coordinates
SCROLL <direction> [<amount>]  ΓÇö scroll up/down/left/right
TYPE <text>          ΓÇö type literal text
OPEN <app-or-file>   ΓÇö open an application or file
CLOSE [<target>]     ΓÇö close the active or named window
HOTKEY <key1> [<key2>...]  ΓÇö press a key combination
DICTATE <text>       ΓÇö paste text verbatim via clipboard
CLARIFY <question>   ΓÇö ask the user to clarify; do not act
SCREENSHOT           ΓÇö capture the desktop screen

Examples (output is the verb followed by the literal value only):
User: click the save button
Assistant: CLICK save button
User: scroll down three times
Assistant: SCROLL down 3
User: close this window
Assistant: CLOSE
User: type hello world
Assistant: TYPE hello world
User: open Chrome browser
Assistant: OPEN Chrome

Rules:
- Reply with ONLY the action string, nothing else.
- Do not explain or comment.
- Do not echo the placeholder notation: no <...>, no quotes, no '=' sign.
- If the request is ambiguous reply with CLARIFY followed by a short question.
- If the request matches no action reply with CLARIFY.
"""


def _build_prompt(
    cmd: Command,
    few_shot_examples: list[dict] | None = None,
    counterexamples: list[dict] | None = None,
) -> str:
    """Build the full prompt sent to the LLM."""
    parts = [_SYSTEM_PROMPT]

    if few_shot_examples:
        parts.append("\nExamples:")
        for ex in few_shot_examples:
            parts.append(f'User: {ex["command_text"]}\nAssistant: {ex["action_text"]}')

    if counterexamples:
        parts.append("\nDo NOT produce these responses:")
        for ex in counterexamples:
            parts.append(f'User: {ex["command_text"]} | Wrong: {ex["wrong_action"]}')

    if cmd.session_context:
        context = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        parts.append(f"\nRecent commands:\n{context}")

    parts.append(f"\nUser: {cmd.text}\nAssistant:")
    return "\n".join(parts)


def _build_chat_messages(
    cmd: Command,
    few_shot_examples: list[dict] | None = None,
    counterexamples: list[dict] | None = None,
) -> list[dict]:
    """Build OpenAI/Ollama-style chat messages for the /api/chat tool path."""
    system_content = _SYSTEM_PROMPT
    if counterexamples:
        neg_block = "Avoid these incorrect mappings:\n" + "\n".join(
            f'"{ex["command_text"]}" -> NOT "{ex["wrong_action"]}"'
            for ex in counterexamples
        )
        system_content += "\n\n" + neg_block
    messages: list[dict] = [{"role": "system", "content": system_content}]
    if few_shot_examples:
        for ex in few_shot_examples:
            messages.append({"role": "user", "content": ex["command_text"]})
            messages.append({"role": "assistant", "content": ex["action_text"]})
    if cmd.session_context:
        ctx = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
        messages.append({"role": "user", "content": f"Recent commands:\n{ctx}"})
        messages.append({"role": "assistant", "content": "Understood."})
    messages.append({"role": "user", "content": cmd.text})
    return messages


# ---------------------------------------------------------------------------
# Native tool-calling schema (Ollama 0.30+ — "tool calling carries over")
# ---------------------------------------------------------------------------
#
# A single constrained tool. The `verb` enum gives the Ollama path the same
# output-format guarantee the vLLM path gets from grammar-constrained decoding
# (VLLMInference._VERB_PATTERN): the model can only emit a valid verb, so the
# default Ollama backend stops relying on the model obeying "reply with ONLY the
# action string". `argument` carries the rest (target / text / keys), so the
# reconstructed "VERB argument" string is byte-compatible with the contract
# HybridCoordinator._parse_action already expects — no downstream changes.
#
# Opt-in via OllamaInference(use_tools=True); the default (generate) path is the
# verified 100%-accuracy backend and is unchanged.

# 11 accessibility verbs + CLARIFY — exactly the set OllamaInference emits
# (dev-agent verbs are routed to DevAgent/ModelRouter before reaching here).
_ACTION_VERBS: list[str] = [
    "CLICK", "MOUSEDOWN", "MOUSEUP", "SCROLL", "TYPE", "OPEN",
    "CLOSE", "HOTKEY", "DICTATE", "CLARIFY", "SCREENSHOT",
]

_DESKTOP_ACTION_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "desktop_action",
        "description": (
            "Emit exactly ONE desktop-control action that fulfils the user's "
            "request. Always call this tool — never reply with prose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verb": {
                    "type": "string",
                    "enum": _ACTION_VERBS,
                    "description": "The single action verb to perform.",
                },
                "argument": {
                    "type": "string",
                    "description": (
                        "The action's argument: the click target, text to type, "
                        "key combo, scroll direction[+amount], app/file to open, "
                        "or the clarifying question for CLARIFY. Empty string for "
                        "SCREENSHOT (and CLOSE of the active window)."
                    ),
                },
            },
            "required": ["verb"],
        },
    },
}


def _action_from_tool_call(tool_call: dict) -> str | None:
    """Reconstruct a 'VERB argument' action string from one Ollama tool_call.

    Returns None when the call isn't our tool or carries no usable verb, so the
    caller can fall back to parsing free-text content.
    """
    fn = tool_call.get("function") or {}
    if fn.get("name") != "desktop_action":
        return None
    args = fn.get("arguments")
    # Ollama returns arguments as a dict; some builds/models stringify it as JSON.
    if isinstance(args, str):
        try:
            import json as _json
            args = _json.loads(args)
        except Exception:
            return None
    if not isinstance(args, dict):
        return None
    verb = str(args.get("verb", "")).strip().upper()
    if verb not in _ACTION_VERBS:
        return None
    argument = str(args.get("argument", "")).strip()
    return f"{verb} {argument}".strip()


def _recover_action_from_content(content: str) -> str | None:
    """Recover a 'VERB argument' action from a model that serialised its tool
    call into the content field instead of using tool_calls.

    Observed with llama3.1 on Ollama 0.30: the model emits a function-call-shaped
    JSON as text, e.g. {"name": "CLARIFY", "parameters": {"verb": "CLARIFY",
    "argument": "..."}}. We tolerate several shapes (verb in the object, in
    nested parameters/arguments, or the function `name` being the verb itself).
    Returns None when no valid verb can be recovered, so the caller can fall
    back to the raw first line.
    """
    content = (content or "").strip()
    if not content:
        return None

    import json as _json
    import re as _re

    objs: list = []
    try:
        objs.append(_json.loads(content))
    except Exception:
        m = _re.search(r"\{.*\}", content, _re.DOTALL)
        if m:
            try:
                objs.append(_json.loads(m.group(0)))
            except Exception:
                pass

    for obj in objs:
        if not isinstance(obj, dict):
            continue
        params = obj.get("parameters") or obj.get("arguments") or {}
        if isinstance(params, str):
            try:
                params = _json.loads(params)
            except Exception:
                params = {}
        params = params if isinstance(params, dict) else {}

        verb = params.get("verb") or obj.get("verb") or obj.get("name")
        verb = str(verb or "").strip().upper()
        if verb not in _ACTION_VERBS:
            continue
        argument = str(params.get("argument", "") or obj.get("argument", "")).strip()
        return f"{verb} {argument}".strip()
    return None


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
        use_tools: bool = False,
    ) -> None:
        self.model = model
        self.host = host
        self.timeout = timeout
        # Opt-in native tool-calling path (/api/chat with a constrained tool).
        # Default off → the verified generate path is unchanged. Requires a
        # tool-capable model + Ollama 0.30+ ("ollama show <model>" → tools cap).
        self.use_tools = use_tools
        self._available: bool | None = None  # None = not yet checked
        # Latched breaker (gap #4): once Ollama looks down, fail fast instead of
        # paying the full timeout on every request until it recovers.
        from core.circuit_breaker import CircuitBreaker
        # slow_call_s = 80% of the call timeout: a backend that keeps barely
        # beating the timeout is degrading — trip the breaker before the user
        # eats near-timeout latency on every request (timeout-aware, #4 tail).
        self._breaker = CircuitBreaker(
            name="ollama", fail_threshold=3, cooldown_s=30.0,
            slow_call_s=max(1.0, timeout * 0.8),
            on_open=self._emit_breaker_open,
        )
        # Serialise requests to this Ollama endpoint. Concurrent calls overwhelm
        # Ollama's GPU-discovery phase and can trigger a runner-spawn storm (observed
        # 2026-06-04). One in-flight request at a time is sufficient; the circuit
        # breaker handles the backend-down fast-fail path.
        self._request_sem = asyncio.Semaphore(1)
        # Optional metrics sink; wired via set_metrics() from main.py or tests.
        self._metrics = None
        # Optional EventBus; wired via set_event_bus() so backend stalls and the
        # breaker opening are visible on the bus (observability batch 2026-06-19).
        self._event_bus = None

    def set_metrics(self, metrics) -> None:
        """Wire an in-process Metrics object for hang-timeout counter updates."""
        self._metrics = metrics

    def set_event_bus(self, bus) -> None:
        """Wire an EventBus so breaker-open and hang events publish (optional)."""
        self._event_bus = bus

    def _emit_bus(self, topic: str, payload: dict) -> None:
        """Fire-and-forget publish on the running loop. No-op without a bus/loop."""
        if self._event_bus is None:
            return
        try:
            from core.async_utils import fire_and_log
            fire_and_log(
                self._event_bus.publish(topic, payload, source="ollama_inference"),
                log, label=f"publish {topic}",
            )
        except Exception:
            pass

    def _emit_breaker_open(self, status: dict) -> None:
        """CircuitBreaker on_open hook → breaker.opened event (runs on the loop)."""
        from core.events import TOPIC_BREAKER_OPENED
        self._emit_bus(TOPIC_BREAKER_OPENED, status)

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
        counterexamples: list[dict] | None = None,
    ) -> str:
        if self.use_tools:
            return await self._infer_tools(cmd, few_shot_examples, counterexamples)

        try:
            import aiohttp
        except ImportError:
            return "CLARIFY aiohttp not installed"

        if not self._breaker.allow():
            return "CLARIFY inference backend unavailable (circuit open)"
        _probe_gen = self._breaker.probe_gen   # tag this probe's outcome (#16)

        prompt = _build_prompt(cmd, few_shot_examples, counterexamples)
        set_inference_capture(prompt)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 64},
        }

        # Outer hang-guard: if Ollama stalls (CUDA OOM, runner deadlock) the
        # _request_sem could block the next caller indefinitely. DA_OLLAMA_TIMEOUT_S
        # (default 45 s) is a hard ceiling covering BOTH semaphore acquisition and
        # the HTTP call. This is wider than self.timeout (10 s aiohttp total) to
        # allow cold-start model loading; tune down if the host is predictably fast.
        _hang_timeout_s = float(os.environ.get("DA_OLLAMA_TIMEOUT_S", "45"))
        t0 = time.monotonic()
        # Report the outcome in `finally` so a CancelledError (which is a
        # BaseException, NOT caught by `except Exception`) still clears the
        # breaker's half-open probe flag — otherwise a cancelled probe wedges
        # the breaker shut forever.
        succeeded = False
        # Track whether semaphore was acquired so the outer TimeoutError handler
        # knows whether the inner finally already updated the breaker.
        _entered_sem = False
        try:
            async with asyncio.timeout(_hang_timeout_s):  # type: ignore[attr-defined]
                async with self._request_sem:
                    _entered_sem = True
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
                                set_inference_capture(
                                    prompt, data.get("prompt_eval_count"), data.get("eval_count")
                                )
                                latency_ms = (time.monotonic() - t0) * 1000
                                log.info("OllamaInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
                                self._available = True
                                succeeded = True
                                return action
                    except Exception as exc:
                        self._available = False
                        # Keep the raw transport error in the log; the user-facing CLARIFY
                        # stays a stable sentence (E16) — never leak aiohttp/SSL internals.
                        log.error("OllamaInference failed: %s", exc)
                        return "CLARIFY the local model is unavailable right now. Please try again."
                    finally:
                        if succeeded:
                            # Pass latency so a slow-but-successful call counts toward the
                            # timeout-aware breaker (#4 tail).
                            self._breaker.record_success(
                                _probe_gen, latency_s=time.monotonic() - t0
                            )
                        else:
                            # Covers both `except Exception` returns and BaseException
                            # (CancelledError/timeout) propagation.
                            self._breaker.record_failure(_probe_gen)
        except asyncio.TimeoutError:
            elapsed_s = time.monotonic() - t0
            log.error(
                "OllamaInference: hang detected — outer timeout fired after %.1fs "
                "(semaphore=%s). Tune DA_OLLAMA_TIMEOUT_S or check Ollama/VRAM health.",
                elapsed_s,
                "acquired" if _entered_sem else "waiting",
            )
            if self._metrics is not None:
                self._metrics.inc("ollama_hang_detected")
            from core.events import TOPIC_INFERENCE_STALLED
            self._emit_bus(TOPIC_INFERENCE_STALLED, {
                "timeout_s": round(_hang_timeout_s, 1),
                "elapsed_s": round(elapsed_s, 1),
                "backend": "ollama",
                "phase": "acquired" if _entered_sem else "waiting",
            })
            # When the timeout fires during semaphore ACQUISITION (_entered_sem=False),
            # the inner finally never ran — update the breaker here so it isn't left
            # with a dangling half-open probe.  When it fires inside the sem block
            # (_entered_sem=True), the inner finally already called record_failure.
            if not _entered_sem:
                self._breaker.record_failure(_probe_gen)
            self._available = False
            return "CLARIFY the local model is unavailable right now. Please try again."

    async def _chat(self, messages: list[dict], tools: list[dict] | None = None,
                    format: dict | None = None) -> dict:
        """POST /api/chat and return the parsed JSON response.

        Isolated so the tool path is unit-testable without mocking aiohttp —
        tests monkeypatch this method with a canned response dict.

        ``format`` is Ollama's structured-output grammar (a JSON Schema dict).
        When provided, the model is constrained to emit conforming JSON — the
        same mechanism the plan profile uses in production via
        ``payload["format"]``. Omitted (None) → byte-identical to the legacy call.
        """
        import aiohttp

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if tools:
            payload["tools"] = tools
        if format is not None:
            payload["format"] = format

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Ollama HTTP {resp.status}")
                return await resp.json()

    async def _infer_tools(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
        counterexamples: list[dict] | None = None,
    ) -> str:
        """Native tool-calling path: /api/chat with the constrained desktop_action tool.

        The `verb` enum constrains output to a valid action verb (the format
        guarantee), and the result is reconstructed into the same "VERB argument"
        string the generate path returns, so HybridCoordinator is unchanged.
        Falls back to parsing free-text content if the model declined the tool.
        """
        try:
            import aiohttp  # noqa: F401 — presence check before _chat uses it
        except ImportError:
            return "CLARIFY aiohttp not installed"

        if not self._breaker.allow():
            return "CLARIFY inference backend unavailable (circuit open)"
        _probe_gen = self._breaker.probe_gen   # tag this probe's outcome (#16)

        messages = _build_chat_messages(cmd, few_shot_examples, counterexamples)
        prompt_json = json.dumps(messages)
        set_inference_capture(prompt_json)
        t0 = time.monotonic()
        # Report the outcome in `finally` so a CancelledError (BaseException, not
        # caught by `except Exception`) still clears the breaker's half-open
        # probe flag — otherwise a cancelled probe wedges the breaker shut.
        succeeded = False
        try:
            data = await self._chat(messages, tools=[_DESKTOP_ACTION_TOOL])
            succeeded = True
        except Exception as exc:
            self._available = False
            log.error("OllamaInference[tools] failed: %s", exc)
            return f"CLARIFY inference error: {exc}"
        finally:
            if succeeded:
                self._breaker.record_success(_probe_gen)
            else:
                self._breaker.record_failure(_probe_gen)

        self._available = True
        set_inference_capture(
            prompt_json, data.get("prompt_eval_count"), data.get("eval_count")
        )
        latency_ms = (time.monotonic() - t0) * 1000
        message = data.get("message", {}) or {}

        # Preferred path: a structured tool call.
        for call in message.get("tool_calls", []) or []:
            action = _action_from_tool_call(call)
            if action:
                log.info("OllamaInference[tools]: %r → %r (%.0f ms)",
                         cmd.text, action, latency_ms)
                return action

        # Fallback: the model answered in content despite the tool. Reuse the
        # generate-path contract (first non-empty line) so behaviour degrades
        # gracefully rather than dropping the command.
        content = (message.get("content") or "").strip()
        if content:
            # Some models serialise the call into content as function-call JSON;
            # recover the action from it, else take the first non-empty line.
            action = _recover_action_from_content(content) or content.splitlines()[0].strip()
            log.info("OllamaInference[tools]: no tool_call, parsed content %r → %r (%.0f ms)",
                     cmd.text, action, latency_ms)
            return action

        log.warning("OllamaInference[tools]: empty response for %r", cmd.text)
        return "CLARIFY no action produced"

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

    async def warmup(self) -> bool:
        """Pre-load the command model into VRAM so the FIRST real command doesn't
        pay a cold-load penalty (~7.5 s observed for llama3.1:8b on a cold 5090,
        vs ~190 ms warm).

        Posts an empty-prompt /api/generate, which Ollama treats as a pure
        model-load request — it returns once the model is resident without
        generating tokens. Best-effort: never raises (CancelledError still
        propagates so shutdown can cancel it), holds the same request semaphore
        as infer() so it can't collide with a concurrent command, and is bounded
        by DA_OLLAMA_TIMEOUT_S to cover a slow cold load. On any failure the
        first command simply loads the model as it does today.
        """
        try:
            import aiohttp
        except ImportError:
            return False

        _hang_timeout_s = float(os.environ.get("DA_OLLAMA_TIMEOUT_S", "45"))
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(_hang_timeout_s):
                async with self._request_sem:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{self.host}/api/generate",
                            json={"model": self.model, "prompt": "", "stream": False},
                            timeout=aiohttp.ClientTimeout(total=_hang_timeout_s),
                        ) as resp:
                            if resp.status != 200:
                                log.warning("OllamaInference.warmup: HTTP %s for %s",
                                            resp.status, self.model)
                                return False
                            await resp.json()
            self._available = True
            log.info("OllamaInference.warmup: %s resident (%.1fs)",
                     self.model, time.monotonic() - t0)
            return True
        except Exception as exc:
            # Best-effort: a failed warm-up must not break startup. The breaker
            # is intentionally NOT touched here so a cold-load hiccup doesn't
            # fail-fast real traffic before it arrives.
            log.warning("OllamaInference.warmup: %s did not warm (%s)", self.model, exc)
            return False

    def get_status(self) -> dict:
        return {
            "backend": "ollama",
            "model": self.model,
            "host": self.host,
            "available": self._available,
            "use_tools": self.use_tools,
        }

