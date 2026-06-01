"""DashScopeAgent — route dev-domain queries to Qwen via Alibaba DashScope.

DashScope exposes an OpenAI-compatible endpoint, so the ``openai`` SDK is used
as the client (no additional Alibaba SDK required).  Domain → model mapping:

  math    → qwq-32b                      (chain-of-thought reasoning)
  code    → qwen2.5-coder-32b-instruct   (code specialist, ~6× cheaper than Sonnet)
  vision  → qwen-vl-max                  (72B+ VLM; cloud fallback for local 30B)
  plan    → qwen3-72b
  general → qwen3-72b

Two ways to use it (mirrors CloudDevAgent, wired in ``main.py`` /
``HybridCoordinator``):
  --dashscope                          → fallback: fires when no local specialist
                                         is GPU-resident.
  --dashscope --no-local-specialists   → primary cloud path; no GPU wake ever.

Domain split with Anthropic CloudDevAgent
-----------------------------------------
By default DashScope handles math / code / vision (Qwen is stronger on maths and
code; Qwen-VL matches or exceeds Sonnet for desktop screenshots).  Anthropic keeps
plan and general (better structured English prose).  Override with
``--dashscope-domains``.

Pricing (DashScope, approximate, at ~1 000 in + 500 out tokens per query):
  qwq-32b                      ~$0.005/query
  qwen2.5-coder-32b-instruct   ~$0.004/query
  qwen-vl-max                  ~$0.006/query
  qwen3-72b                    ~$0.003/query

QwQ-32B thinking
-----------------
DashScope surfaces QwQ's reasoning trace in ``<think>…</think>`` tags inside the
content string.  ``_strip_thinking()`` removes them so only the final answer
reaches the user / TTS pipeline.

This module degrades gracefully: if the ``openai`` SDK is missing or
``DASHSCOPE_API_KEY`` is not set, ``get_status()`` reports ``available=False``
and ``run()`` returns a ``"CLARIFY …"`` string instead of raising.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

try:
    from openai import AsyncOpenAI
    _HAS_OPENAI = True
except ImportError:
    AsyncOpenAI = None  # type: ignore
    _HAS_OPENAI = False

log = logging.getLogger(__name__)

_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Default model per domain.  Keys mirror DomainClassifier output labels.
_DOMAIN_MODELS: dict[str, str] = {
    "math":    "qwq-32b",
    "code":    "qwen2.5-coder-32b-instruct",
    "vision":  "qwen-vl-max",
    "plan":    "qwen3-72b",
    "general": "qwen3-72b",
}

_DEFAULT_DOMAIN = "general"

# QwQ-32B wraps its chain-of-thought in <think>…</think>.  Strip it so the user
# and TTS only receive the final answer text.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# ---------------------------------------------------------------------------
# Domain system prompts — self-contained; tuned for a graduate researcher in
# ML/AI, quantum computing, and applied mathematics.
# ---------------------------------------------------------------------------

_CODE_PROMPT = """\
You are an expert software engineer for a graduate researcher in ML/AI, quantum \
computing, and scientific computing — fluent in PyTorch, JAX, HuggingFace, vLLM, \
Qiskit, PennyLane, NumPy/SciPy/Sympy, Rust, C++, CUDA/Triton, and async Python.

Output rules (strict):
- Reply with ONE code block and nothing else — no preamble, no postamble.
- Add a short note after the code block only if the user explicitly asks to \
"explain", "why", or "walk through".
- Use the simplest correct approach; do not over-engineer.
- Modern idiomatic style: type hints, dataclasses, async where appropriate."""

_MATH_PROMPT = """\
You are a mathematical reasoning assistant for a graduate researcher in machine \
learning theory, quantum computing, and applied mathematics.

- Work step by step; show all non-trivial algebraic steps.
- State assumptions and domains explicitly.
- Use LaTeX: inline $...$ or display $$...$$.
- If a result connects to a named theorem, name it."""

_VISION_PROMPT = """\
You are analysing a desktop screenshot for a software developer and graduate \
researcher (IDE code, a research paper, an ML training dashboard, a terminal, \
maths notes, or a quantum circuit). If no image is attached, answer from the \
text description provided.

- Read and extract visible text accurately; transcribe equations in LaTeX.
- Identify the application and context.
- Answer the user's specific question with technical precision."""

_PLAN_PROMPT = """\
You are a senior software architect and ML/QC research engineer. Produce a \
concrete, numbered action plan for the user's goal.

- One action per numbered step; be specific (exact file paths, exact commands).
- Cover the task end to end, including tests and a final commit where sensible.
- This plan is advisory — describe each step clearly so the user (or the local \
agent) can execute it. Do not claim to have run anything."""

_GENERAL_PROMPT = """\
You are a knowledgeable research assistant for a graduate student in machine \
learning, agentic AI, quantum computing, and applied mathematics.

- Technically precise; assume graduate-level background.
- Use LaTeX for maths ($...$ inline, $$...$$ display).
- For "how does X work" questions, explain the mechanism, not just a definition.
- Concise — skip preamble and caveats unless they materially affect the answer."""

_SYSTEM_PROMPTS: dict[str, str] = {
    "code":    _CODE_PROMPT,
    "math":    _MATH_PROMPT,
    "vision":  _VISION_PROMPT,
    "plan":    _PLAN_PROMPT,
    "general": _GENERAL_PROMPT,
}

# File paths mentioned in a query — Windows drive paths, posix-ish paths, or a
# bare filename with a known source extension.
_PATH_RE = re.compile(
    r"""[A-Za-z]:\\[^\s'"]+"""
    r"""|(?:[\w.\-]+/)+[\w.\-]+\.\w+"""
    r"""|\b[\w.\-]+\.(?:py|swift|md|txt|json|ya?ml|js|ts|rs|cpp|cc|cu|toml|cfg)\b""",
    re.VERBOSE,
)


def _extract_paths(text: str) -> list[str]:
    seen: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        tok = m.group(0)
        if tok not in seen:
            seen.append(tok)
    return seen


def _strip_thinking(text: str) -> str:
    """Remove QwQ <think>…</think> blocks, leaving only the final answer."""
    return _THINK_RE.sub("", text).strip()


class DashScopeAgent:
    """Route dev-domain queries (math/code/vision/plan/general) to Qwen via
    DashScope's OpenAI-compatible API.

    Designed to complement CloudDevAgent: DashScope handles math, code, and
    vision by default; Anthropic handles plan and general.  Domain coverage is
    configurable via ``HybridCoordinator.set_dashscope_agent(domains=…)``.

    Pricing: ~$0.003–$0.006 per dev query depending on model.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = None  # lazily constructed AsyncOpenAI

    # ---------------------------------------------------------------------- #
    # Client lifecycle
    # ---------------------------------------------------------------------- #

    def _get_client(self) -> "AsyncOpenAI":
        """Lazily construct the async OpenAI client pointing at DashScope."""
        if self._client is None:
            if not _HAS_OPENAI:
                raise RuntimeError("openai SDK not installed (pip install openai)")
            if not self._api_key:
                raise RuntimeError("DASHSCOPE_API_KEY not set")
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
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
        """Run a single dev-domain query against a Qwen model and return the answer.

        On any error (missing SDK/key, network, API error) returns a
        ``"CLARIFY …"`` string so the caller's downstream handling is unchanged.
        """
        context = context or {}
        if domain not in _SYSTEM_PROMPTS:
            domain = _DEFAULT_DOMAIN

        try:
            client = self._get_client()
        except Exception as exc:
            log.warning("DashScopeAgent unavailable: %s", exc)
            return "CLARIFY Qwen cloud dev agent temporarily unavailable"

        model = _DOMAIN_MODELS.get(domain, _DOMAIN_MODELS[_DEFAULT_DOMAIN])
        messages = self._build_messages(query, domain, context)

        try:
            completion = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=self._max_tokens,
            )
            raw = completion.choices[0].message.content or ""
            text = _strip_thinking(raw)
            log.info(
                "DashScopeAgent[%s]: %d chars  model=%s  finish=%s  tokens=%s",
                domain, len(text), model,
                completion.choices[0].finish_reason,
                getattr(completion, "usage", None),
            )
            return text or "CLARIFY Qwen cloud dev agent returned no text"
        except Exception as exc:
            log.error("DashScopeAgent.run failed (domain=%s model=%s): %s", domain, model, exc)
            return "CLARIFY Qwen cloud dev agent temporarily unavailable"

    def _build_messages(self, query: str, domain: str, context: dict) -> list[dict]:
        """Assemble the messages list: system prompt + optional image + user turn."""
        system_text = _SYSTEM_PROMPTS[domain]
        messages: list[dict] = [{"role": "system", "content": system_text}]

        user_content: list[dict] = []

        # Vision: attach screenshot as an OpenAI-format image_url block.
        shot = context.get("screenshot_b64")
        if shot and domain == "vision":
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{shot}"},
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

        text_body = (
            "[Context]\n" + "\n".join(ctx_lines) + "\n\n[Request]\n" + query
            if ctx_lines else query
        )
        user_content.append({"type": "text", "text": text_body})

        messages.append({"role": "user", "content": user_content})
        return messages

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    @property
    def model(self) -> str:
        """Representative model label (used in log / status table)."""
        return " / ".join(sorted(set(_DOMAIN_MODELS.values())))

    def get_status(self) -> dict:
        return {
            "available": bool(_HAS_OPENAI and self._api_key),
            "models": dict(_DOMAIN_MODELS),
            "domain_count": len(_SYSTEM_PROMPTS),
            "backend": "dashscope",
        }
