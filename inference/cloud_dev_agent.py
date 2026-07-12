"""CloudDevAgent — route dev-domain queries to Claude via Amazon Bedrock.

Drop-in alternative / fallback for the local ``VLLMSpecialistPool`` + ``DevAgent``
path.  Routing a code/math/vision/plan/general query to the cloud avoids waking a
local 30B specialist (≈50 s cold load + a full teardown of the command engine on
the 32 GB RTX 5090), keeping the GPU free for the latency-critical command path.

Two ways to use it (wired in ``main.py`` / ``HybridCoordinator``):
  --cloud-dev-agent                         → cloud is a *fallback*: dev queries go
                                              local when a specialist is already
                                              awake, else to the cloud.
  --cloud-dev-agent --no-local-specialists  → cloud is the *primary* dev path; the
                                              specialist pool is never created and
                                              no GPU specialist is ever woken.

Surface choice — Messages API, not Managed Agents
-------------------------------------------------
This uses the standard Messages API (``client.messages.stream``) rather than the
Managed Agents API.  The dev queries here are single-turn — generate a function,
derive a result, answer a screen-grounding question — and return plain text that
matches ``DevAgent.AgentResult.response_text``.  Managed Agents (server-managed
stateful sessions with a per-session container + SSE event loop) would add a
container, an event stream, and a different return shape for no benefit on a
stateless, text-returning path.  Per the Anthropic surface decision tree:
"single LLM call → Claude API; one request, one response."

Pricing
-------
At ~1000 input + ~500 output tokens per dev query, Claude Opus 4.8
($5 / $25 per MTok) costs ~$0.0175 per query
(0.005 in + 0.0125 out ≈ $0.0175, plus a little for the system prompt + adaptive
thinking on math/plan).  The Managed Agents session-hour fee ($0.08/hr) does NOT
apply — the plain Messages API is billed by token only.

This module degrades gracefully: if the ``anthropic`` SDK is missing or no API
key is configured, ``get_status()`` reports ``available=False`` and ``run()``
returns a ``CLARIFY ...`` string instead of raising.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:  # graceful degradation — never crash the import
    anthropic = None  # type: ignore
    _HAS_ANTHROPIC = False

log = logging.getLogger(__name__)

# Default model — Claude Opus 4.8 ($5/$25 per MTok): most capable model for
# free-form dev generation and long-horizon agentic reasoning.  Override via the
# constructor.  Supports adaptive thinking (math/plan domains).
# (On Amazon Bedrock this needs per-model access granted on the account; a fresh
# grant can lag the runtime a few minutes — the dev path CLARIFYs until it lands.)
_DEFAULT_MODEL = "claude-opus-4-8"

# Domains whose answer benefits from reasoning.  Adaptive thinking is enabled
# only here, mirroring the local ModelRouter profiles (math/plan think; code/
# vision/general don't) so simple-domain latency stays low.
_THINKING_DOMAINS = {"math", "plan"}

_DEFAULT_DOMAIN = "general"

# ---------------------------------------------------------------------------
# Domain system prompts — self-contained (no import from the local inference
# stack, so CloudDevAgent works standalone).  Tuned for a graduate researcher
# in ML/AI, quantum computing, and applied mathematics.
# ---------------------------------------------------------------------------

_CODE_PROMPT = """\
[IDENTITY]
You are an expert Software Engineer (The Builder) for a graduate researcher in ML/AI, quantum \
computing, and scientific computing — fluent in PyTorch, JAX, HuggingFace, vLLM, \
Qiskit, PennyLane, NumPy/SciPy/Sympy, Rust, C++, CUDA/Triton, and async Python.

[PRIME DIRECTIVE]
Generate the simplest correct code to satisfy the user's goal. Do not over-engineer.

[STRICT CONSTRAINTS]
- Do NOT include a preamble or postamble.
- Do NOT explain the code unless explicitly asked to "explain", "why", or "walk through".

[FORMAT]
- Reply with ONE code block and nothing else.
- Modern idiomatic style: type hints, dataclasses, async where appropriate."""

_MATH_PROMPT = """\
[IDENTITY]
You are a mathematical reasoning assistant for a graduate researcher in machine \
learning theory, quantum computing, and applied mathematics.

[PRIME DIRECTIVE]
Solve mathematical problems step by step, showing all non-trivial algebraic steps.

[STRICT CONSTRAINTS]
- Do not leave assumptions or domains implicit; state them explicitly.
- Do not omit the chain of thought.

[FORMAT]
- Use LaTeX: inline $...$ or display $$...$$.
- If a result connects to a named theorem, name it."""

_VISION_PROMPT = """\
[IDENTITY]
You are analysing a desktop screenshot for a software developer and graduate \
researcher (IDE code, a research paper, an ML training dashboard, a terminal, \
maths notes, or a quantum circuit).

[PRIME DIRECTIVE]
Extract text accurately and answer the specific question with technical precision.

[STRICT CONSTRAINTS]
- Do not hallucinate text.
- Do not provide unrequested generic descriptions.

[FORMAT]
- Read and extract visible text accurately; transcribe equations in LaTeX.
- Identify the application and context.
- If no image is attached, answer from the text description provided."""

_PLAN_PROMPT = """\
[IDENTITY]
You are a senior software architect and ML/QC research engineer.

[PRIME DIRECTIVE]
Produce a concrete, numbered action plan for the user's goal.

[STRICT CONSTRAINTS]
- Do not claim to have run anything (the plan is advisory).
- Do not omit requested steps (like tests or commits).

[FORMAT]
- One action per numbered step; be specific (exact file paths, exact commands).
- Cover the task end to end, including tests and a final commit where sensible.
- Describe each step clearly so the user or local agent can execute it."""

_GENERAL_PROMPT = """\
[IDENTITY]
You are a knowledgeable research assistant for a graduate student in machine \
learning, agentic AI, quantum computing, and applied mathematics.

[PRIME DIRECTIVE]
Answer questions with technical precision and appropriate depth.

[STRICT CONSTRAINTS]
- Do not give long conversational preambles or generic caveats unless material.
- Do not just define concepts for "how does X work" questions; explain the mechanism.

[FORMAT]
- Assume graduate-level background.
- Use LaTeX for maths ($...$ inline, $$...$$ display).
- Be concise."""

_SYSTEM_PROMPTS: dict[str, str] = {
    "code": _CODE_PROMPT,
    "math": _MATH_PROMPT,
    "vision": _VISION_PROMPT,
    "plan": _PLAN_PROMPT,
    "general": _GENERAL_PROMPT,
}

# File paths mentioned in a query — Windows drive paths, posix-ish paths, or a
# bare filename with a known source extension.  Best-effort; used only as extra
# context, so false negatives are harmless.
_PATH_RE = re.compile(
    r"""[A-Za-z]:\\[^\s'"]+"""                              # C:\foo\bar.py
    r"""|(?:[\w.\-]+/)+[\w.\-]+\.\w+"""                     # foo/bar/baz.py
    r"""|\b[\w.\-]+\.(?:py|swift|md|txt|json|ya?ml|js|ts|rs|cpp|cc|cu|toml|cfg)\b""",
    re.VERBOSE,
)


def _extract_paths(text: str) -> list[str]:
    """Return distinct file-path-like tokens mentioned in ``text`` (order-stable)."""
    seen: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen


def _extract_text(message) -> str:
    """Concatenate the text blocks of a Messages API response (drops thinking)."""
    parts = [
        b.text for b in getattr(message, "content", [])
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip()


class CloudDevAgent:
    """Route dev-domain queries (code/math/vision/plan/general) to Claude via
    Amazon Bedrock with adaptive thinking on the reasoning-heavy domains.

    Used as a fallback when local specialists are unavailable/sleeping, or as the
    primary dev path with ``--cloud-dev-agent --no-local-specialists``.

    Pricing: token-only on the Messages API — ~$0.0175 per dev query at
    ~1000 in / ~500 out on Opus 4.8 ($5/$25 per MTok). No session-hour fee.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: int = 2048,      # dev answers need room (vs the command path's 64)
        timeout: float = 60.0,
    ) -> None:
        # Model resolution: explicit arg → DA_CLOUD_DEV_MODEL env override → default.
        # The env override is a no-code-change knob to flip the dev model at runtime
        # — e.g. temporarily fall back to Sonnet 4.6 while a fresh Opus 4.8 Bedrock
        # grant propagates to the inference layer, then unset it to return to Opus.
        resolved = model or os.environ.get("DA_CLOUD_DEV_MODEL") or _DEFAULT_MODEL
        self.model = resolved
        self._base_model = resolved       # backend-mapped at client-build time
        self._backend = "bedrock"
        self._max_tokens = max_tokens
        self._timeout = timeout
        # `api_key` is accepted for signature stability but ignored: this project
        # accesses Claude exclusively through Amazon Bedrock, whose credential is
        # the AWS_BEARER_TOKEN_BEDROCK env var resolved in core.cloud_backend.
        self._client = None  # lazily constructed AsyncAnthropicBedrock
        self._rate_limiter = None  # shared 'anthropic' bucket; wired by coordinator

    def set_rate_limiter(self, limiter) -> None:
        """Share the coordinator's RateLimiter so dev + command cloud calls
        count against one 'anthropic' bucket. Fail-open if never wired."""
        self._rate_limiter = limiter

    # ---------------------------------------------------------------------- #
    # Client lifecycle
    # ---------------------------------------------------------------------- #

    def _get_client(self):
        """Lazily construct the async client (raises with an actionable message).

        Routes through core.cloud_backend so the dev path shares the command
        path's Amazon Bedrock backend. The credential is the
        AWS_BEARER_TOKEN_BEDROCK env var; resolve_backend() raises a clear,
        actionable message when it is missing.
        """
        if self._client is None:
            if not _HAS_ANTHROPIC:
                raise RuntimeError("anthropic SDK not installed (pip install anthropic)")
            from core.cloud_backend import resolve_backend
            backend = resolve_backend()
            self._client = backend.make_client(async_=True, timeout=self._timeout)
            self._backend = backend.name
            self.model = backend.map_model(self._base_model)
        return self._client

    # ---------------------------------------------------------------------- #
    # Inference
    # ---------------------------------------------------------------------- #

    async def run(
        self,
        query: str,
        domain: str = "general",
        context: Optional[dict] = None,
    ) -> str:
        """Run a single dev-domain query against Claude and return the answer text.

        On any error (missing SDK/key, network, API error) returns a
        ``"CLARIFY cloud dev agent unavailable: {error}"`` string so the caller's
        downstream handling (display / TTS) is unchanged and never hard-fails.
        """
        context = context or {}
        if domain not in _SYSTEM_PROMPTS:
            domain = _DEFAULT_DOMAIN
        system_text = _SYSTEM_PROMPTS[domain]

        try:
            client = self._get_client()
        except Exception as exc:
            log.warning("CloudDevAgent unavailable: %s", exc)
            return "CLARIFY cloud dev agent temporarily unavailable"

        # Throttle cloud egress (shares the 'anthropic' bucket with _run_cloud).
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.check("anthropic")
            except Exception as exc:
                log.debug("CloudDevAgent rate-limit check skipped: %s", exc)

        user_content = self._build_user_content(query, domain, context)

        # Adaptive thinking only where the reasoning trace adds value (math/plan).
        thinking = {"type": "adaptive"} if domain in _THINKING_DOMAINS else {"type": "disabled"}

        # The per-domain system prompt is a stable prefix → mark it cacheable.
        # (These prompts sit below Sonnet 4.6's ~2048-token minimum cacheable
        # prefix, so this is currently a no-op, but it's correct and harmless and
        # starts caching automatically if the prompts grow.)
        system = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]

        try:
            # Stream for responsiveness; get_final_message() consumes the stream
            # and returns the assembled Message (with usage) for logging.
            async with client.messages.stream(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                thinking=thinking,
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                message = await stream.get_final_message()
            text = _extract_text(message)
            usage = getattr(message, "usage", None)
            log.info(
                "CloudDevAgent[%s]: %d chars  model=%s  stop=%s  usage=%s",
                domain, len(text), self.model,
                getattr(message, "stop_reason", "?"), usage,
            )
            # Publish token usage on the task-local capture so the coordinator can
            # persist it to the `inferences` table (cost ledger). These dev calls
            # run on Opus 4.8 — the most expensive cloud path — so leaving them
            # unrecorded was the biggest blind spot in cloud spend. The coordinator
            # clears the capture before calling run(), mirroring the command path.
            try:
                from inference.local_inference import set_inference_capture
                set_inference_capture(
                    None,
                    getattr(usage, "input_tokens", None) if usage else None,
                    getattr(usage, "output_tokens", None) if usage else None,
                )
            except Exception:
                pass
            return text or "CLARIFY cloud dev agent returned no text"
        except Exception as exc:
            # anthropic.APIError subclasses (auth/rate-limit/server) + transport
            # errors all funnel here.  Match the local CLARIFY error contract.
            log.error("CloudDevAgent.run failed (domain=%s): %s", domain, exc)
            return "CLARIFY cloud dev agent temporarily unavailable"

    def _build_user_content(self, query: str, domain: str, context: dict) -> list[dict]:
        """Assemble the user turn: optional screenshot + context preamble + query."""
        blocks: list[dict] = []

        # Vision: include a screenshot if the caller supplied one (the dev intercept
        # usually won't, so vision degrades to a text-only technical answer).
        shot = context.get("screenshot_b64")
        if shot and domain == "vision":
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": shot},
            })

        ctx_lines: list[str] = []
        sid = context.get("session_id")
        if sid is not None and sid != -1:
            ctx_lines.append(f"Session ID: {sid}")
        recent = context.get("recent_commands") or []
        if recent:
            ctx_lines.append(
                "Recent commands:\n" + "\n".join(f"  - {c}" for c in recent[-5:])
            )
        paths = _extract_paths(query)
        if paths:
            ctx_lines.append("File paths mentioned: " + ", ".join(paths))

        if ctx_lines:
            text = "[Context]\n" + "\n".join(ctx_lines) + "\n\n[Request]\n" + query
        else:
            text = query
        blocks.append({"type": "text", "text": text})
        return blocks

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        # Available if the SDK is present AND an Amazon Bedrock credential
        # (AWS_BEARER_TOKEN_BEDROCK) is configured.
        available = False
        if _HAS_ANTHROPIC:
            try:
                from core.cloud_backend import credential_available
                available = credential_available()
            except Exception:
                pass
        return {
            "available": available,
            "model": self.model,
            "domain_count": len(_SYSTEM_PROMPTS),
            "backend": self._backend,
        }
