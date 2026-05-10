"""ModelRouter — VRAM-aware specialist model selection and inference.

Maps each domain to a specialist Ollama model with a domain-tuned system
prompt. Handles the different output formats each model produces (structured
verb-first for the command classifier, free-form for reasoning/code/vision).

Model lineup (RTX 5090, 31.8 GB, baseline 8.3 GB, Whisper 4.2 GB):
  command  → llama3.2:3b     (2.0 GB)  fast action classifier, verb-first output
  code     → qwen3-coder:30b (18 GB)   code generation, ML/QC frameworks
  math     → deepseek-r1:8b  (5.2 GB)  chain-of-thought reasoning, proofs
  vision   → qwen3-vl:30b    (19 GB)   screenshot analysis, diagram reading
  plan     → gpt-oss:20b     (13 GB)   project decomposition, structured plans
  general  → gpt-oss:20b     (13 GB)   explanation, research synthesis

Each specialist model uses a domain-specific system prompt calibrated for
ML/agentic AI, quantum computing, and software development contexts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain-specific system prompts
# ---------------------------------------------------------------------------

_COMMAND_PROMPT = """\
You are a desktop control assistant. Convert the user's request into exactly ONE \
action from this vocabulary:
CLICK <target> | SCROLL <dir> [n] | TYPE <text> | OPEN <app> | CLOSE [target] | \
HOTKEY <keys> | DICTATE <text> | CLARIFY <question> | SCREENSHOT | \
WRITE_FILE <path> | RUN_TERMINAL <cmd> | EXPLAIN <text> | SEARCH_WEB <query> | READ_SCREEN
Reply with ONLY the action string. No explanation."""

_CODE_PROMPT = """\
You are an expert software engineer specialising in machine learning, agentic AI, \
quantum computing, and scientific computing. Your user is a graduate researcher in these fields.

Domains you are fluent in:
- ML/AI: PyTorch, JAX, Flax, HuggingFace Transformers, vLLM, LangChain, LangGraph, \
  AutoGen, diffusion models, RL (stable-baselines3, Gymnasium)
- Quantum: Qiskit, PennyLane, Cirq, quantum gates, variational algorithms (VQE, QAOA), \
  quantum error correction, Clifford circuits
- Scientific: NumPy, SciPy, Matplotlib, Sympy, Pandas, NetworkX
- Systems: Rust, C++, CUDA, Triton, async Python, Docker

When writing code:
- Prefer modern idiomatic patterns (dataclasses, type hints, async where appropriate)
- Include brief inline comments only for non-obvious logic
- Default to PyTorch for ML unless otherwise specified
- Output a single clean code block unless explicitly asked for explanation

When explaining:
- Be technically precise; assume graduate-level background
- Use LaTeX notation for math (wrap in $ or $$)"""

_MATH_PROMPT = """\
You are a mathematical reasoning assistant for a graduate researcher in machine learning \
theory, quantum computing, and applied mathematics.

Core areas: linear algebra, multivariate calculus, probability theory, information theory, \
convex optimisation, differential geometry, quantum mechanics, group theory.

When solving problems:
- Work step by step; show all non-trivial algebraic steps
- State assumptions and domains explicitly
- Use LaTeX: inline $...$ or display $$...$$
- When multiple approaches exist, note the most illuminating one
- If a result connects to a known theorem, name it

Think through the problem fully before presenting the solution."""

_VISION_PROMPT = """\
You are analysing a desktop screenshot for a software developer and graduate researcher.
The user may be looking at: code in an IDE, a research paper, a ML training dashboard, \
a terminal, mathematical notes, or a quantum circuit diagram.

When analysing:
- Read and extract all visible text accurately
- Identify the application and context
- Answer the user's specific question with technical precision
- If you see code, describe its language and purpose
- If you see equations, transcribe them in LaTeX
- If you see a plot or diagram, describe axes, data, and what it represents"""

_PLAN_PROMPT = """\
You are a software project architect helping a graduate ML/QC researcher plan and execute \
complex development tasks.

When given a goal, produce a concrete numbered action plan. Each step must be one of:
  [WRITE_FILE <path>] — create or edit a file (follow with the content)
  [RUN_TERMINAL <cmd>] — execute a shell command
  [CLICK <target>] — click a UI element
  [OPEN <app>] — open an application
  [SEARCH_WEB <query>] — search the web
  [EXPLAIN <text>] — note something for the user

Rules:
- Be specific: include exact file paths, exact commands, exact content
- One action per step
- Cover the full task end to end
- Prefer: uv/pip for Python packages, git for version control, pytest for tests
- Default Python environment: Windows, .venv, pyproject.toml

Format:
Step 1: [ACTION args]
<any file content or detail needed>
Step 2: [ACTION args]
..."""

_GENERAL_PROMPT = """\
You are a knowledgeable research assistant for a graduate student in machine learning, \
agentic AI, quantum computing, and applied mathematics.

Be technically precise, concise, and assume graduate-level background.
Use LaTeX for math ($...$ inline, $$...$$ display).
Cite relevant papers, frameworks, or tools when applicable.
If asked to compare approaches, be opinionated about tradeoffs."""


# ---------------------------------------------------------------------------
# Model profiles
# ---------------------------------------------------------------------------

@dataclass
class ModelProfile:
    name: str
    domain: str
    system_prompt: str
    vram_gb: float
    max_tokens: int = 1024
    supports_images: bool = False
    # For models that wrap reasoning in tags (deepseek-r1)
    strip_thinking: bool = False
    # Models that don't follow strict verb-first format
    free_form: bool = False

    def __str__(self) -> str:
        return f"{self.name} ({self.domain}, {self.vram_gb}GB)"


_PROFILES: dict[str, ModelProfile] = {
    "command": ModelProfile(
        name="llama3.2:3b",
        domain="command",
        system_prompt=_COMMAND_PROMPT,
        vram_gb=2.0,
        max_tokens=32,
        free_form=False,
    ),
    "code": ModelProfile(
        name="qwen3-coder:30b",
        domain="code",
        system_prompt=_CODE_PROMPT,
        vram_gb=18.0,
        max_tokens=2048,
        free_form=True,
    ),
    "math": ModelProfile(
        name="deepseek-r1:8b",
        domain="math",
        system_prompt=_MATH_PROMPT,
        vram_gb=5.2,
        max_tokens=2048,
        strip_thinking=False,  # Keep reasoning for math — it's the value
        free_form=True,
    ),
    "vision": ModelProfile(
        name="qwen3-vl:30b",
        domain="vision",
        system_prompt=_VISION_PROMPT,
        vram_gb=19.0,
        max_tokens=1024,
        supports_images=True,
        free_form=True,
    ),
    "plan": ModelProfile(
        name="gpt-oss:20b",
        domain="plan",
        system_prompt=_PLAN_PROMPT,
        vram_gb=13.0,
        max_tokens=2048,
        free_form=True,
    ),
    "general": ModelProfile(
        name="gpt-oss:20b",
        domain="general",
        system_prompt=_GENERAL_PROMPT,
        vram_gb=13.0,
        max_tokens=1024,
        free_form=True,
    ),
}

# Fallback chain per domain when preferred model won't fit in VRAM
_FALLBACK: dict[str, list[str]] = {
    "code":    ["qwen3-coder:30b", "gpt-oss:20b", "llama3.1:8b", "llama3.2:3b"],
    "math":    ["deepseek-r1:8b",  "gpt-oss:20b", "llama3.1:8b", "llama3.2:3b"],
    "vision":  ["qwen3-vl:30b",   "gpt-oss:20b", "llama3.1:8b", "llama3.2:3b"],
    "plan":    ["gpt-oss:20b",    "llama3.1:8b", "llama3.2:3b"],
    "general": ["gpt-oss:20b",    "llama3.1:8b", "llama3.2:3b"],
    "command": ["llama3.2:3b",    "llama3.1:8b"],
}


# ---------------------------------------------------------------------------
# VRAM check
# ---------------------------------------------------------------------------

def _free_vram_gb() -> float:
    try:
        import pynvml as nvml
        nvml.nvmlInit()
        h = nvml.nvmlDeviceGetHandleByIndex(0)
        info = nvml.nvmlDeviceGetMemoryInfo(h)
        nvml.nvmlShutdown()
        return info.free / (1024 ** 3)
    except Exception:
        return 999.0  # assume unlimited if can't check


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouter:
    """Select the right model for a domain and run inference against Ollama."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        timeout: float = 120.0,
    ) -> None:
        self._host = host
        self._timeout = timeout
        self._profiles = _PROFILES.copy()

    # ---------------------------------------------------------------------- #
    # Model selection
    # ---------------------------------------------------------------------- #

    def select_profile(self, domain: str) -> ModelProfile:
        """Choose the best available profile for the domain given current VRAM."""
        free_gb = _free_vram_gb()
        chain = _FALLBACK.get(domain, ["llama3.2:3b"])

        for model_name in chain:
            # Find profile for this model name
            profile = next(
                (p for p in self._profiles.values() if p.name == model_name),
                None,
            )
            if profile is None:
                continue
            if profile.vram_gb <= free_gb + 2.0:  # 2 GB tolerance for VRAM fluctuation
                log.info(
                    "ModelRouter: domain=%s → %s (%.1f GB, %.1f GB free)",
                    domain, model_name, profile.vram_gb, free_gb,
                )
                return profile

        # Ultimate fallback
        log.warning("ModelRouter: no profile fits VRAM, falling back to llama3.2:3b")
        return self._profiles["command"]

    # ---------------------------------------------------------------------- #
    # Inference
    # ---------------------------------------------------------------------- #

    async def infer(
        self,
        domain: str,
        user_text: str,
        screenshot_b64: Optional[str] = None,
        context: Optional[str] = None,
    ) -> "RouterResult":
        """Run inference with the selected specialist model.

        Args:
            domain:         Classified domain string.
            user_text:      The user's query or command text.
            screenshot_b64: Base64 PNG for vision requests.
            context:        Optional recent session context (last few messages).

        Returns:
            RouterResult with response text, model used, and latency.
        """
        profile = self.select_profile(domain)

        prompt_parts = [profile.system_prompt]
        if context:
            prompt_parts.append(f"\nRecent context:\n{context}")
        prompt_parts.append(f"\nUser: {user_text}\nAssistant:")
        prompt = "\n".join(prompt_parts)

        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self._call_ollama,
                profile,
                prompt,
                screenshot_b64,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            log.info(
                "ModelRouter: %s → %s (%.0f ms) [domain=%s]",
                profile.name, repr(response[:80]), latency_ms, domain,
            )
            return RouterResult(
                text=response,
                model=profile.name,
                domain=domain,
                latency_ms=latency_ms,
                free_form=profile.free_form,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            log.error("ModelRouter inference failed (%s): %s", profile.name, exc)
            return RouterResult(
                text=f"CLARIFY inference error: {exc}",
                model=profile.name,
                domain=domain,
                latency_ms=latency_ms,
                free_form=False,
                error=str(exc),
            )

    def _call_ollama(
        self,
        profile: ModelProfile,
        prompt: str,
        screenshot_b64: Optional[str],
    ) -> str:
        payload: dict = {
            "model": profile.name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0 if not profile.free_form else 0.3,
                "num_predict": profile.max_tokens,
            },
        }
        if screenshot_b64 and profile.supports_images:
            payload["images"] = [screenshot_b64]

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._host}/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())

        raw = data.get("response", "").strip()

        # Strip deepseek-r1 thinking tags only for non-math domains
        # (for math we keep the reasoning; for command/plan we don't want it)
        if "<think>" in raw and profile.domain not in ("math",):
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # Take first non-empty line for command domain
        if not profile.free_form:
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            return lines[0] if lines else ""

        return raw

    # ---------------------------------------------------------------------- #
    # Vision shortcut
    # ---------------------------------------------------------------------- #

    async def analyse_screen(self, screenshot_b64: str, question: str) -> "RouterResult":
        """Convenience method for screen analysis queries."""
        return await self.infer(
            domain="vision",
            user_text=question,
            screenshot_b64=screenshot_b64,
        )

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        free = _free_vram_gb()
        available = {
            domain: p.name
            for domain, p in self._profiles.items()
            if p.vram_gb <= free + 2.0
        }
        return {
            "free_vram_gb": round(free, 1),
            "available_specialists": available,
        }


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RouterResult:
    text: str
    model: str
    domain: str
    latency_ms: float
    free_form: bool
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def first_line(self) -> str:
        lines = [l.strip() for l in self.text.splitlines() if l.strip()]
        return lines[0] if lines else ""
