"""ModelRouter — VRAM-aware specialist model selection and inference.

Maps each domain to a specialist Ollama model with a domain-tuned system
prompt. Handles the different output formats each model produces (structured
verb-first for the command classifier, free-form for reasoning/code/vision).

Model lineup (RTX 5090, 32 GB VRAM — updated 2026-05-25):
  command  → llama3.1:8b      (4.6 GB)  fast action classifier, verb-first output, 100% accuracy
  code     → qwen3-coder:30b  (17.3 GB) MoE (3.3B active/token); thinking mode ON; code gen, ML/QC
  math     → deepseek-r1:8b   (4.9 GB)  R1-distill-Qwen3-8B; chain-of-thought reasoning, proofs
  vision   → qwen3-vl:30b     (18.2 GB) multimodal; screenshot analysis, diagram reading
  plan     → qwen3-coder:30b  (17.3 GB) thinking mode ON; far better plans than 8B; stays warm
  general  → gemma3:27b       (16.2 GB) dense 27B, 128K ctx; research synthesis, explanation

Key decisions:
  - plan uses qwen3-coder:30b (not gemma3:27b) so code+plan share one loaded model → fewer swaps
  - general uses gemma3:27b (dense, broader knowledge) rather than the coding-specialist qwen3
  - thinking mode enabled for code and plan (qwen3-coder native support via RENDERER qwen3-coder)
  - deepseek-r1:8b is Qwen3-arch (DeepSeek-R1-Distill-Qwen-8B); family=qwen3 in Ollama
  - Removed from router: gpt-oss:20b (0% accuracy), nemotron-mini (25%), qwen2.5-coder:1.5b-base (base)

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
You are a senior software architect and ML/QC research engineer helping a graduate researcher \
plan and autonomously execute complex development tasks. Think through the task carefully before \
producing the plan.

When given a goal, produce a concrete numbered action plan. Each step must use exactly one \
of the following action verbs:

File operations:
  [WRITE_FILE <path>]        — create or overwrite a file (put content on the next lines)
  [READ_FILE <path>]         — read an existing file into context
  [GREP <pattern> [<path>]]  — search for a pattern across the codebase

Shell / terminal:
  [RUN_TERMINAL <command>]   — execute a shell command; capture stdout/stderr

Git operations:
  [GIT_STATUS]               — show working tree status (staged/unstaged files, branch)
  [GIT_DIFF [--staged|<file>]] — show diff of working changes
  [GIT_COMMIT <message>]     — stage all tracked changes and commit
  [GIT_CHECKOUT [-b] <branch>] — checkout or create a branch

GitHub:
  [GITHUB_PR <title>]        — create a pull request (put PR body on the next lines)

Web / research:
  [FETCH_URL <url>]          — fetch URL and extract text for context
  [SEARCH_WEB <query>]       — open browser with search (use FETCH_URL for programmatic retrieval)

Desktop / UI:
  [CLICK <target>]           — click a named UI element
  [OPEN <app>]               — launch an application
  [HOTKEY <keys>]            — press a keyboard shortcut

Output / explain:
  [EXPLAIN <text>]           — note something for the user (no action taken)
  [READ_SCREEN]              — take a screenshot and analyse it

Rules:
- Think step by step before producing the plan
- Be specific: exact file paths, exact commands, exact content
- One action per numbered step; content (file body, PR description) follows the step line
- Cover the full task end to end, including tests and git commit when appropriate
- Python environment: Windows, .venv, pytest, pyproject.toml or requirements.txt
- For ML tasks: default to PyTorch unless JAX/Qiskit/PennyLane is explicitly needed
- For git tasks: run GIT_STATUS first, commit at the end

Format:
Step 1: [ACTION args]
<optional content / detail for this step>
Step 2: [ACTION args]
..."""

_GENERAL_PROMPT = """\
You are a knowledgeable research assistant for a graduate student in machine learning, \
agentic AI, quantum computing, and applied mathematics.

Core expertise:
- ML/AI: deep learning theory, transformers, diffusion models, RL, LLM alignment, agentic systems
- Quantum computing: gate-based QC, VQE, QAOA, quantum error correction, Clifford circuits
- Math: linear algebra, probability, information theory, convex optimisation, differential geometry
- Systems: Python, Rust, CUDA, async architecture, cloud ML (AWS, GCP)

Style:
- Technically precise; assume graduate-level background
- Use LaTeX for math ($...$ inline, $$...$$ display)
- Cite relevant papers (arXiv, conference proceedings) when applicable
- If comparing approaches, give a direct opinionated recommendation with tradeoff reasoning
- For "how does X work" questions: explain the mechanism, not just the definition
- Concise — skip preamble and caveats unless they materially affect the answer"""


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
    # For models that wrap reasoning in tags (deepseek-r1 / qwen3 thinking)
    strip_thinking: bool = False
    # Models that don't follow strict verb-first format
    free_form: bool = False
    # Enable Qwen3-native thinking mode (RENDERER qwen3-coder / qwen3)
    # Passes {"think": true} in Ollama options — produces <think>...</think> prefix
    thinking: bool = False

    def __str__(self) -> str:
        think_flag = " [thinking]" if self.thinking else ""
        return f"{self.name} ({self.domain}, {self.vram_gb}GB{think_flag})"


_PROFILES: dict[str, ModelProfile] = {
    # ── Fast command classifier ────────────────────────────────────────────
    "command": ModelProfile(
        name="llama3.1:8b",
        domain="command",
        system_prompt=_COMMAND_PROMPT,
        vram_gb=4.6,
        max_tokens=32,
        free_form=False,
        # Benchmarked: 100% accuracy on 12-prompt suite, 373ms warm p50
    ),
    # ── Code specialist: Qwen3-Coder 30B MoE, thinking ON ─────────────────
    # qwen3-coder:30b is MoE (30.5B params, ~3.3B active/token).
    # Thinking mode via RENDERER qwen3-coder — passes think=True to Ollama.
    # Stays warm between code and plan requests → no VRAM swap penalty.
    "code": ModelProfile(
        name="qwen3-coder:30b",
        domain="code",
        system_prompt=_CODE_PROMPT,
        vram_gb=17.3,
        max_tokens=4096,
        free_form=True,
        thinking=True,       # native Qwen3 thinking mode; strips <think> for code output
        strip_thinking=True, # remove <think> block from final response (keep the answer only)
    ),
    # ── Math: DeepSeek-R1-Distill-Qwen3-8B ───────────────────────────────
    # Family = qwen3 (Qwen3 architecture, R1 distillation). Keep reasoning visible.
    "math": ModelProfile(
        name="deepseek-r1:8b",
        domain="math",
        system_prompt=_MATH_PROMPT,
        vram_gb=4.9,
        max_tokens=4096,
        strip_thinking=False,  # Keep the chain-of-thought — it IS the math value
        free_form=True,
    ),
    # ── Vision: Qwen3-VL 30B ─────────────────────────────────────────────
    "vision": ModelProfile(
        name="qwen3-vl:30b",
        domain="vision",
        system_prompt=_VISION_PROMPT,
        vram_gb=18.2,
        max_tokens=2048,
        supports_images=True,
        free_form=True,
    ),
    # ── Plan: Qwen3-Coder 30B MoE, thinking ON ───────────────────────────
    # Same model as code — stays warm → zero model-swap cost when code was recent.
    # Thinking mode produces dramatically better multi-step plans than 8B without thinking.
    # Updated prompt includes all current verbs: GIT_STATUS, GITHUB_PR, FETCH_URL, etc.
    "plan": ModelProfile(
        name="qwen3-coder:30b",
        domain="plan",
        system_prompt=_PLAN_PROMPT,
        vram_gb=17.3,
        max_tokens=4096,
        free_form=True,
        thinking=True,
        strip_thinking=True,  # plan response = the steps, not the reasoning trace
    ),
    # ── General: Gemma 3 27B (dense, 128K ctx) ───────────────────────────
    # Dense 27B vs MoE 30B: Gemma 3 is better for broad research synthesis,
    # explanation, and cross-domain questions where depth > code specialisation.
    # 16.2 GB — fits with 8+ GB headroom alongside Whisper (4.2 GB).
    "general": ModelProfile(
        name="gemma3:27b",
        domain="general",
        system_prompt=_GENERAL_PROMPT,
        vram_gb=16.2,
        max_tokens=2048,
        free_form=True,
    ),
}

# Fallback chain per domain when preferred model won't fit in VRAM.
# Ordered: preferred → smaller capable → smallest available.
# Removed from all chains: gpt-oss:20b (0% accuracy), nemotron-mini (25%),
# qwen2.5-coder:1.5b-base (base model, no instruction following).
_FALLBACK: dict[str, list[str]] = {
    "code":    ["qwen3-coder:30b", "gemma3:27b",   "llama3.1:8b", "llama3.2:3b"],
    "math":    ["deepseek-r1:8b",  "llama3.1:8b",  "llama3.2:3b"],
    "vision":  ["qwen3-vl:30b",    "gemma3:27b",   "llama3.1:8b", "llama3.2:3b"],
    "plan":    ["qwen3-coder:30b", "gemma3:27b",   "llama3.1:8b", "llama3.2:3b"],
    "general": ["gemma3:27b",      "llama3.1:8b",  "llama3.2:3b"],
    "command": ["llama3.1:8b",     "llama3.2:3b"],
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
        chain = _FALLBACK.get(domain, ["llama3.1:8b"])

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
        log.warning("ModelRouter: no profile fits VRAM, falling back to llama3.1:8b")
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
        options: dict = {
            "temperature": 0.0 if not profile.free_form else 0.3,
            "num_predict": profile.max_tokens,
        }

        # Qwen3 native thinking mode — supported by qwen3-coder:30b (RENDERER qwen3-coder).
        # Ollama passes this as an option to the model; the model emits <think>...</think>
        # before its answer. We strip or keep the think block per profile.strip_thinking.
        if profile.thinking:
            options["think"] = True

        payload: dict = {
            "model": profile.name,
            "prompt": prompt,
            "stream": False,
            "options": options,
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

        # Strip <think>...</think> blocks when profile.strip_thinking is True.
        # Keep them for math (the chain-of-thought IS the answer).
        # Also strip any partial open tag if model was cut short.
        if profile.strip_thinking and "<think>" in raw:
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            raw = re.sub(r"<think>.*$", "", raw, flags=re.DOTALL).strip()

        # Take first non-empty line for command domain (verb-first format)
        if not profile.free_form:
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
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
            domain: str(p)
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
