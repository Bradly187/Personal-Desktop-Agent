"""ModelRouter — VRAM-aware specialist model selection and inference.

Maps each domain to a specialist model with a domain-tuned system prompt.
Supports two backends:

  Ollama (default, --backend ollama):
    HTTP calls to a local Ollama server.  Simple, no WSL required.
    Cold-load latency: ~60s per 30B model swap.

  vLLM specialist pool (--backend vllm, activated via ModelRouter.set_vllm_pool()):
    In-process vLLM LLM instances with INT4 AWQ quantization.
    Cold-load: ~30-60s first time (weights downloaded + loaded).
    Warm wake from DRAM: ~3-8s (192 GB DRAM → 32 GB VRAM via PCIe).
    TTL sleep after 60s idle: frees VRAM for the next specialist.

Model lineup — VERIFIED Ollama defaults (RTX 5090, 32 GB VRAM):
  command  → llama3.1:8b     GGUF Q4   ~4.6 GB  verb-first; 100% on command eval (2026-05-13)
  code     → qwen3-coder:30b GGUF Q4   ~18 GB   thinking ON (trace stripped from output)
  math     → deepseek-r1:8b  FP16      ~4.9 GB  chain-of-thought kept (it IS the answer)
  vision   → qwen3-vl:30b    GGUF Q4   ~19 GB   multimodal screen grounding
  plan     → qwen3-coder:30b GGUF Q4   ~18 GB   thinking ON (trace stripped)
  general  → gemma4:12b      GGUF Q4   ~9.1 GB  research synthesis; co-resides w/ command+Whisper (2026-06-07)

These are the models the default (Ollama) backend actually runs. A heavy 30B
specialist does not co-reside with Whisper + the command model on 32 GB, so the
ResourceGovernor evicts the resident specialist on a flare (keep_alive=0; see
ModelRouter.heavy_model_names).

PLANNED / opt-in — Gemma 4 single-instance consolidation (NOT the default):
  The vLLM specialist pool (_DEFAULT_SPECIALIST_CONFIGS, --backend vllm) was
  intended to serve all specialist domains from one Gemma 4 instance. Those
  checkpoints are UNVERIFIED and the 31B-dense does not fit 32 GB, so the default
  profiles above use verified Ollama models and the vLLM path falls back to
  Ollama. To opt in once the weights are validated, re-point the specialist
  profiles' `name` at "gemma4:31b" (the pool config key already exists).

VRAM reality with 32 GB RTX 5090:
  Ollama GGUF Q4_K_M ≈ HuggingFace AWQ INT4 (both are ~4-bit; same size).
  30B AWQ models are 16-18 GB — they do NOT fit alongside the command model.
  The pool sleeps the command model first (+1-2s), then wakes the specialist
  (+3-8s) — still 5-10s total vs Ollama's 60s cold load.

  baseline + Whisper:      12.5 GB
  command model (sleeping): ~0 GB  (weights in 192 GB DRAM)
  one active 30B AWQ:      16-18 GB
  total:                   28.5-30.5 GB  ✓ (1.5-3.5 GB KV headroom)

  deepseek-r1:8b (~4-5 GB) is the only specialist that co-resides with
  the command model without sleeping it.

HuggingFace AWQ checkpoints (exact IDs verified 2026-05-30):
  code/plan: QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ    (community, needs verification)
  math:      deepseek-ai/DeepSeek-R1-Distill-Qwen-8B        (FP16, 4.9 GB, no AWQ needed)
  vision:    QuantTrio/Qwen3-VL-30B-A3B-Instruct-AWQ        (community, needs verification)
  general:   gaunernst/gemma-3-27b-it-int4-awq              (community, needs verification)

  Qwen3 note: official Qwen models have an 'A3B' suffix (Active-3B for MoE routing).
  Both -Coder and -VL variants. The base repos are:
    Qwen/Qwen3-Coder-30B-A3B-Instruct
    Qwen/Qwen3-VL-30B-A3B-Instruct

Key decisions:
  - plan shares qwen3-coder:30b → zero extra wake cost after a code request
  - deepseek-r1:8b stays FP16 (4.9 GB, comfortably fits; INT4 degrades math reasoning)
  - thinking mode stripped for code/plan output; kept for math (it IS the answer)
  - general → gemma4:12b (2026-06-07): 100% on the reasoning probe (ties gemma3:27b) at
    +9.1 GB vs +16 GB, so it co-resides with command+Whisper and the ResourceGovernor no
    longer needs a ~16 GB swap for general queries. Pain-day fallback is gemma4:e4b-it-qat
    (+5.1 GB, "general_light" profile); gemma3:27b is retired (kept pulled for rollback).
    NOTE: Gemma 4 always reasons internally — max_tokens must be generous (>=4096) or the
    response truncates to empty (done_reason=length). Ollama reads `think` ONLY as a
    top-level request param; `options["think"]` is silently ignored (so the current
    thinking flag is a no-op on the Ollama path — see _call_ollama). Default mode returns
    clean answers, which is why general uses thinking=False + a large max_tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from monitoring.trace import get_tracer

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
You are an expert software engineer for a graduate researcher in ML/AI, quantum computing, \
and scientific computing. You are fluent in PyTorch, JAX, HuggingFace, vLLM, Qiskit, PennyLane, \
Cirq, NumPy/SciPy/Sympy/Pandas, Rust, C++, CUDA, Triton, and async Python.

OUTPUT RULES (strict — follow exactly):
- Reply with ONE code block and nothing else. No preamble, no postamble, no commentary.
- Do NOT explain, justify, or describe the code unless the user EXPLICITLY asks to \
"explain", "why", or "walk through" — then add a short note after the code block.
- Use the SIMPLEST correct approach. Do not over-engineer or suggest advanced/alternative \
algorithms unless the user asks for them.
- Modern idiomatic style: type hints, dataclasses, async where appropriate. Default to PyTorch for ML.
- Inline comments only for genuinely non-obvious logic; keep them terse."""

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
- One action per step; multi-line content (file body, PR description) goes in `body`
- Cover the full task end to end, including tests and git commit when appropriate
- Python environment: Windows, .venv, pytest, pyproject.toml or requirements.txt
- For ML tasks: default to PyTorch unless JAX/Qiskit/PennyLane is explicitly needed
- For git tasks: run GIT_STATUS first, commit at the end
- PARALLELISM: by default steps run in order. If a step depends ONLY on specific
  earlier steps, list their 1-based positions in `after` — independent steps with
  an empty `after` may then run concurrently. Reads and writes to DISTINCT files
  are independent; anything sharing the shell, git, or one file must declare the
  dependency. When unsure, leave `after` empty (it runs sequentially — safe).

Output ONLY a JSON object of this exact shape (no prose, no code fences):
{
  "steps": [
    {"action": "<one verb above>", "args": "<arguments, may be empty>",
     "body": "<multi-line content e.g. file contents, may be empty>",
     "after": [<1-based positions of steps this depends on>]}
  ]
}
Steps are 1-based by their order in the array; `after` references those positions."""

# Verb vocabulary the planner may emit (mirrors DevAgent._PLAN_ACTIONS).
_PLAN_VERBS: list[str] = [
    "WRITE_FILE", "RUN_TERMINAL", "READ_FILE", "GREP",
    "GIT_STATUS", "GIT_DIFF", "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR",
    "FETCH_URL", "SEARCH_WEB", "CLICK", "OPEN", "HOTKEY", "SCROLL", "TYPE",
    "EXPLAIN", "READ_SCREEN",
]

# Ollama `format` JSON Schema constraining the plan response (gap: free-text
# parsing). An object with a `steps` array; each step is action/args/body/after.
_PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": _PLAN_VERBS},
                    "args": {"type": "string"},
                    "body": {"type": "string"},
                    "after": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["steps"],
}

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
    strip_thinking: bool = False  # strip <think>/<thinking> reasoning trace from output
    free_form: bool = False       # output is free-form prose, not verb-first command
    # Enable thinking / reasoning mode.
    # Ollama path: passes {"think": true} in options (Qwen3/Gemma4 Ollama renderers)
    # vLLM path:  passes extra_body={"enable_thinking": True} in SamplingParams
    # Gemma 4:    thinking API needs validation — may differ from Qwen3 extra_body
    thinking: bool = False
    # Cluster offload: when set, _call_ollama targets this base URL instead of
    # the router's local self._host (e.g. the laptop node's Ollama). None = local.
    inference_host: Optional[str] = None
    # Structured output (Ollama `format`): when set, _call_ollama passes this
    # JSON Schema so the model's response is constrained to valid JSON. Used by
    # the plan profile so the planner emits a parseable step array instead of
    # free text (eliminates the regex body-collision / arg-truncation bug class).
    json_schema: Optional[dict] = None

    def __str__(self) -> str:
        think_flag = " [thinking]" if self.thinking else ""
        return f"{self.name} ({self.domain}, {self.vram_gb}GB{think_flag})"


_PROFILES: dict[str, ModelProfile] = {
    # ── Command classifier — Gemma 4 E4B-IT (4.5B dense) ─────────────────
    # Primary path: VLLMInference with grammar-constrained decoding.
    # This profile is used only for the Ollama fallback path.
    "command": ModelProfile(   # VERIFIED — Ollama default for the command domain
        name="llama3.1:8b",
        domain="command",
        system_prompt=_COMMAND_PROMPT,
        vram_gb=4.6,
        max_tokens=32,
        free_form=False,
    ),
    # ── Lightweight command path — offloaded to the laptop node ───────────
    # Same verb-first contract as "command", but runs llama3.1:8b on the
    # laptop's Ollama (RTX 4070) to free desktop VRAM/CPU. inference_host is
    # filled in at runtime by ModelRouter.set_cluster() from cluster_config.json.
    # Used for the command domain when the laptop is configured AND healthy;
    # falls back to the local command path on any remote error.
    "lightweight": ModelProfile(
        name="llama3.1:8b",
        domain="command",
        system_prompt=_COMMAND_PROMPT,
        vram_gb=4.6,
        max_tokens=32,
        free_form=False,
        inference_host=None,   # set by ModelRouter.set_cluster()
    ),
    # ── Specialist domains — distinct VERIFIED Ollama models ───────────────
    # Per-domain specialists (not a single shared instance — that's the PLANNED
    # gemma4 vLLM path). thinking=True triggers Ollama option {"think": true};
    # strip_thinking removes <think>…</think> (Qwen3/DeepSeek) traces from output.
    # VERIFIED Ollama specialist profiles (see module docstring). thinking is ON
    # where the reasoning trace adds value: code/plan (stripped from output) and
    # math (kept — the CoT IS the answer).
    "code": ModelProfile(
        name="qwen3-coder:30b",
        domain="code",
        system_prompt=_CODE_PROMPT,
        vram_gb=18.0,
        max_tokens=1536,
        free_form=True,
        thinking=True,
        strip_thinking=True,   # remove the <think> trace from code output
    ),
    "math": ModelProfile(
        name="deepseek-r1:8b",
        domain="math",
        system_prompt=_MATH_PROMPT,
        vram_gb=4.9,           # FP16 (INT4 degrades math reasoning)
        max_tokens=2048,
        free_form=True,
        thinking=True,         # KEEP: chain-of-thought IS the math answer
        strip_thinking=False,
    ),
    "vision": ModelProfile(
        name="qwen3-vl:30b",
        domain="vision",
        system_prompt=_VISION_PROMPT,
        vram_gb=19.0,
        max_tokens=1024,
        supports_images=True,
        free_form=True,
        thinking=False,
    ),
    "plan": ModelProfile(
        name="qwen3-coder:30b",
        domain="plan",
        system_prompt=_PLAN_PROMPT,
        vram_gb=18.0,
        max_tokens=2048,
        free_form=True,
        thinking=True,         # planning benefits from reasoning
        strip_thinking=True,
        json_schema=_PLAN_JSON_SCHEMA,   # constrain output to a parseable step array
    ),
    "general": ModelProfile(
        name="gemma4:12b",     # 2026-06-07: 100% reasoning probe, +9.1 GB (co-resides w/ command+Whisper)
        domain="general",
        system_prompt=_GENERAL_PROMPT,
        vram_gb=9.1,
        # Gemma 4 reasons internally before answering; too small a budget truncates the
        # response to empty (done_reason=length). 4096 absorbs the reasoning + a long
        # synthesis answer. thinking=False → default mode (clean answers; options["think"]
        # is a no-op on Ollama anyway). strip_thinking guards any stray inline <think>.
        max_tokens=4096,
        free_form=True,
        thinking=False,
        strip_thinking=True,
    ),
    # Pain-day / low-VRAM general fallback. 100% on the reasoning probe at just +5.1 GB
    # (in _LIGHT_MODELS → co-resides, never evicted). Resolved by name from the general
    # fallback chain; 8/10 on the hard coding eval (fine for a downgrade, not a primary).
    "general_light": ModelProfile(
        name="gemma4:e4b-it-qat",
        domain="general",
        system_prompt=_GENERAL_PROMPT,
        vram_gb=5.1,
        max_tokens=4096,
        free_form=True,
        thinking=False,
        strip_thinking=True,
    ),
}

# Fallback chain per domain (VERIFIED Ollama models, largest-first).
# select_profile works down the list until a model fits in free VRAM; the last
# entry always fits so there is a guaranteed selection. To opt into the PLANNED
# gemma4 consolidation, prepend "gemma4:31b" here and re-point the profiles.
_FALLBACK: dict[str, list[str]] = {
    # Mid-tier fallback repointed gemma3:27b → gemma4:12b (2026-06-07): the resident
    # general model is lighter (9.1 vs 16 GB) and codes well (9-10/10 hard eval), so it's
    # a strictly better "qwen3-coder didn't fit" fallback. gemma3:27b is retired (kept
    # pulled for git-revert rollback only).
    "code":    ["qwen3-coder:30b", "gemma4:12b",  "llama3.1:8b"],
    "math":    ["deepseek-r1:8b",  "llama3.1:8b"],
    "vision":  ["qwen3-vl:30b",    "gemma4:12b",  "llama3.1:8b"],
    "plan":    ["qwen3-coder:30b", "gemma4:12b",  "llama3.1:8b"],
    "general": ["gemma4:12b", "gemma4:e4b-it-qat", "llama3.1:8b"],
    "command": ["llama3.1:8b",     "llama3.2:3b"],
}

# Models small enough to co-reside with the command model + Whisper on 32 GB —
# never worth evicting during a flare. Everything else that can be selected is
# treated as a heavy specialist (see ModelRouter.heavy_model_names).
_LIGHT_MODELS: frozenset[str] = frozenset({
    "llama3.1:8b", "llama3.2:3b", "gemma4:4b", "deepseek-r1:8b",
    # gemma4:e4b-it-qat (+5.1 GB) co-resides with command+Whisper — never worth evicting.
    # gemma4:12b (+9.1 GB) is the general default but is intentionally NOT light: it must
    # remain evictable so a heavy 30B specialist (qwen3-coder/qwen3-vl) can load on demand.
    "gemma4:e4b-it-qat",
})


# ---------------------------------------------------------------------------
# VRAM check
# ---------------------------------------------------------------------------

def _free_vram_gb() -> float:
    # Delegates to the shared VRAM probe (core.vram) so every component reads the
    # SAME signal. Behaviour is identical to the prior inline pynvml call,
    # including the 999.0 fail-open sentinel when VRAM can't be measured.
    from core.vram import free_vram_gb
    return free_vram_gb()


# ---------------------------------------------------------------------------
# VLLMSpecialistPool — INT4 specialist pool with TTL sleep management
# ---------------------------------------------------------------------------

@dataclass
class SpecialistConfig:
    """Per-model vLLM configuration for the specialist pool.

    hf_model_id: HuggingFace model ID (AWQ checkpoint recommended for 30B models).
    quantization: "awq", "gptq", "bitsandbytes", or None (FP16).
                  AWQ/GPTQ require a pre-quantized HF checkpoint.
                  bitsandbytes quantizes FP16 weights at load time (no special checkpoint).
    gpu_util:     Fraction of TOTAL VRAM this engine may use.  With 32 GB VRAM,
                  0.30 = 9.6 GB — enough for a 30B INT4 model + KV cache.
    max_model_len: Max context length.  30B specialists default to 8192.
    dtype:        "auto" lets vLLM pick based on the checkpoint's config.
    """
    hf_model_id: str
    quantization: Optional[str] = None
    gpu_util: float = 0.30
    max_model_len: int = 4096
    dtype: str = "auto"
    enforce_eager: bool = True   # skip CUDA graph compile; saves 0.45-0.63 GB VRAM + 2-min wait


# Default specialist configurations.  Override via VLLMSpecialistPool(configs=...).
_DEFAULT_SPECIALIST_CONFIGS: dict[str, SpecialistConfig] = {
    # ── Gemma 4 31B-IT — single model for all specialist domains ──────────
    # Dense 31B (not MoE), native multimodal, 256K context, thinking mode.
    # AWQ INT4 checkpoint: ~15-16 GB VRAM.
    #
    # Benchmarks (2026-05-30):
    #   HumanEval 80%  (+51 pts vs Gemma 3 27B)
    #   AIME 2026  89%  (+68 pts — thinking mode)
    #   GPQA Diamond 84%  (+42 pts)
    #   τ2-bench agentic 86%  (+79 pts)
    #
    # AWQ checkpoint: community-maintained — verify before pulling:
    #   hf models ls --search "gemma-4-31B-it-AWQ"
    # Alternative if AWQ unavailable: base google/gemma-4-31B-it with
    #   quantization="bitsandbytes" and load_format="bitsandbytes"
    #
    # KV cache safety: max_model_len=4096 avoids the AWQ KV overflow bug
    # (CPU spike / GPU idle on long sequences).  Raise to 8192 after the
    # bug is resolved in a future vLLM release.
    # ── Gemma 4 26B A4B (MoE) — RECOMMENDED for 32 GB VRAM ──────────────────
    # 26B total params, 3.8B active/token (MoE routing). AWQ ~12-13 GB.
    # Fits alongside Whisper (4.2 GB): 8.3 + 4.2 + 13 = 25.5 GB (6.5 GB KV headroom).
    # Verify checkpoint name before download: hf models ls --search "gemma-4-26B-A4B AWQ"
    # cyankiwi: same author as E4B-IT command model (both verified working).
    # 26B total params, 3.8B active/token (MoE). AWQ 4-bit ~12-13 GB.
    # Fits alongside Whisper: 8.3 + 4.2 + 13 = 25.5 GB (6.5 GB KV headroom).
    "gemma4:31b": SpecialistConfig(
        hf_model_id="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        quantization=None,    # auto-detect from model config (may be compressed-tensors)
        gpu_util=0.92,        # MEASURED 2026-05-30: weights load at 16.62 GB (NOT ~13).
                              # With the command engine ASLEEP + Whisper resident, 0.72 gave
                              # NEGATIVE KV (-2.4 GB); 0.92 gives positive KV (3.9-13.8 GB
                              # depending on how much the command sleep freed). Use 0.92.
                              #
                              # RESOLVED via DESTROY-RECREATE (2026-05-30): vLLM-sleep is NOT
                              # used (two concurrent enable_sleep_mode/CuMem engines die with
                              # "CUDA Error: device not ready at cumem_allocator.cpp:187" on
                              # Blackwell). Instead the pool fully tears down the command engine
                              # before loading a specialist, and tears down the specialist when
                              # the command engine reloads. Only ONE engine is GPU-resident at a
                              # time, none use CuMem. Cost: ~50s reload per command<->dev switch.
        max_model_len=4096,
        enforce_eager=True,
    ),
    # ── Gemma 4 31B dense — DOES NOT FIT on 32 GB alongside Whisper ──────────
    # Weights = 19.77 GB + 8.3 GB baseline + 4.2 GB Whisper = 32.27 GB > 32 GB.
    # Keep this entry commented out as a reminder; use 26B A4B above instead.
    # "gemma4:31b-dense": SpecialistConfig(
    #     hf_model_id="QuantTrio/gemma-4-31B-it-AWQ",
    #     quantization=None,
    #     gpu_util=0.57,
    #     max_model_len=4096,
    # ),
}


class VLLMSpecialistPool:
    """Manages one vLLM LLM instance per specialist model with TTL-based VRAM sleep.

    Only one specialist is active (awake) at a time — the pool sleeps the
    previous model before waking the next, so VRAM headroom is always respected.

    VRAM layout (32 GB RTX 5090):
      30B specialists (~16-18 GB AWQ) require sleeping the command model first.
      The pool's _activate() handles this: sleep command → wake specialist.
      Total domain-swap latency: ~5-10s vs Ollama's ~60s cold load.
      deepseek-r1:8b (~5 GB FP16) is the only specialist that skips the swap.

    Wake latency (192 GB DRAM → 32 GB VRAM, PCIe 5.0 x16 ~64 GB/s):
        8B  model:  ~0.5-1s
        30B AWQ:    ~3-8s   (vs Ollama cold load ~60s)

    Sleeping models hold their weights in 192 GB DRAM.  All four specialists
    can sleep simultaneously (~60 GB combined — well within budget).
    """

    _TTL_S: float = 60.0         # sleep after this many idle seconds
    _WATCHDOG_INTERVAL_S: float = 10.0

    def __init__(
        self,
        configs: Optional[dict[str, SpecialistConfig]] = None,
        command_engine: Optional[Any] = None,
    ) -> None:
        self._configs: dict[str, SpecialistConfig] = configs or _DEFAULT_SPECIALIST_CONFIGS.copy()
        self._engines: dict[str, Any] = {}          # model_name → LLM instance
        self._sleeping: set[str] = set()            # models whose weights are in DRAM
        self._last_used: dict[str, float] = {}
        self._active: Optional[str] = None          # currently awake specialist (if any)
        self._lock = asyncio.Lock()
        self._watchdog_task: Optional[asyncio.Task] = None
        # The command-domain VLLMInference engine (E4B).  A 30B-class specialist
        # cannot fit alongside the awake command engine + Whisper on 32 GB, so we
        # sleep the command engine before waking a specialist.  Mutual exclusion
        # the other direction is handled by command_engine's pre-wake hook
        # (wired to self.sleep_all_specialists in main.py).
        self._command_engine = command_engine

    async def start(self) -> None:
        self._watchdog_task = asyncio.create_task(self._sleep_watchdog())
        log.info("VLLMSpecialistPool: started  models=%s", list(self._configs))

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

    async def sleep_all_specialists(self) -> None:
        """Tear down every loaded specialist so the command engine can reclaim VRAM.

        Wired into the command engine's pre-wake hook (see main.py) to enforce
        mutual exclusion: when the command model is about to load, no specialist
        may remain GPU-resident.  We DESTROY rather than vLLM-sleep so no CuMem
        allocator lingers (a sleeping CuMem engine conflicts with the next engine
        on Blackwell).  Safe to call concurrently — guarded by _lock.
        """
        async with self._lock:
            for name in list(self._engines):
                self._teardown_specialist(name)
            self._active = None

    def _teardown_specialist(self, name: str) -> None:
        """Blocking: fully release a specialist engine + EngineCore and free VRAM.

        Must be called under self._lock.  Removes the engine entirely so the next
        _activate() reloads it fresh (~50s cold) — the cost of avoiding the CuMem
        coexistence conflict.
        """
        engine = self._engines.pop(name, None)
        self._sleeping.discard(name)
        if self._active == name:
            self._active = None
        if engine is None:
            return
        try:
            shutdown = getattr(getattr(engine, "llm_engine", None), "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception as exc:
            log.debug("VLLMSpecialistPool: %s shutdown noop/err: %s", name, exc)
        del engine
        try:
            import gc
            gc.collect()
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        log.info("VLLMSpecialistPool: unloaded %s — VRAM freed", name)

    # ---------------------------------------------------------------------- #
    # Core inference
    # ---------------------------------------------------------------------- #

    async def infer(
        self,
        model_name: str,
        profile: "ModelProfile",
        user_text: str,
        screenshot_b64: Optional[str] = None,
        context: Optional[str] = None,
    ) -> str:
        """Run chat inference with the named specialist model.

        Handles load / sleep / wake transparently.  Raises RuntimeError if
        the model is not in self._configs (caller falls back to Ollama).
        """
        if model_name not in self._configs:
            raise RuntimeError(f"VLLMSpecialistPool: no config for {model_name!r}")

        try:
            from vllm import SamplingParams
        except ImportError:
            raise RuntimeError("vllm not installed")

        async with self._lock:
            engine = await self._activate(model_name)

        messages = self._build_messages(profile, user_text, screenshot_b64, context)

        sampling = SamplingParams(
            temperature=0.0 if not profile.free_form else 0.3,
            max_tokens=profile.max_tokens,
        )

        # Thinking mode (Qwen3 / Gemma 4) is toggled via the chat template, NOT
        # SamplingParams — offline vLLM rejects extra_body (an OpenAI-server-only
        # field).  Pass enable_thinking through chat_template_kwargs; vLLM ignores
        # unknown template kwargs for models whose template doesn't use them.
        chat_kwargs: dict[str, Any] = dict(sampling_params=sampling, use_tqdm=False)
        # Pass enable_thinking EXPLICITLY (both True and False). Omitting it lets
        # Gemma 4's chat template default to thinking-on, whose long reasoning
        # trace dominates latency (~78s even warm). Explicit False disables it.
        chat_kwargs["chat_template_kwargs"] = {"enable_thinking": profile.thinking}

        t0 = time.monotonic()
        try:
            outputs = await asyncio.to_thread(
                engine.chat,
                [messages],
                **chat_kwargs,
            )
        except Exception as exc:
            log.error("VLLMSpecialistPool.infer failed (%s): %s", model_name, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000
        async with self._lock:
            self._last_used[model_name] = time.monotonic()

        raw = outputs[0].outputs[0].text.strip() if outputs and outputs[0].outputs else ""

        if profile.strip_thinking:
            raw = self._strip_thinking(raw)

        if not profile.free_form:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            raw = lines[0] if lines else ""

        log.info("VLLMSpecialistPool: %s → %r (%.0f ms)", model_name, raw[:80], latency_ms)
        return raw

    # ---------------------------------------------------------------------- #
    # Lifecycle helpers
    # ---------------------------------------------------------------------- #

    async def _activate(self, model_name: str) -> Any:
        """Ensure model_name is loaded and is the only GPU-resident engine.

        Must be called under self._lock.  Tears down the command engine and any
        other specialist first (destroy-recreate, not vLLM-sleep) to keep exactly
        one engine resident and avoid the Blackwell CuMem coexistence conflict.
        """
        # Tear down the command engine first — a 30B-class specialist + the
        # loaded command engine + Whisper exceed 32 GB.  No-op if not loaded.
        if self._command_engine is not None:
            try:
                await self._command_engine.sleep()  # full unload (see VLLMInference.sleep)
            except Exception as _cmd_exc:
                log.warning("VLLMSpecialistPool: command engine unload failed: %s", _cmd_exc)

        # Tear down any other loaded specialist (only one specialist resident).
        for other in list(self._engines):
            if other != model_name:
                self._teardown_specialist(other)

        if model_name not in self._engines:
            config = self._configs[model_name]
            log.info("VLLMSpecialistPool: loading %s (%s)...", model_name, config.hf_model_id)
            t0 = time.monotonic()
            self._engines[model_name] = await asyncio.to_thread(
                self._blocking_load, config
            )
            log.info("VLLMSpecialistPool: loaded %s in %.1fs", model_name, time.monotonic() - t0)

        self._active = model_name
        self._last_used[model_name] = time.monotonic()
        return self._engines[model_name]

    @staticmethod
    def _blocking_load(config: SpecialistConfig) -> Any:
        from vllm import LLM
        kwargs: dict[str, Any] = dict(
            model=config.hf_model_id,
            gpu_memory_utilization=config.gpu_util,
            max_model_len=config.max_model_len,
            dtype=config.dtype,
            trust_remote_code=False,  # Gemma 4 AWQ: vLLM native support, no custom model code needed
            enforce_eager=config.enforce_eager,
            # NOTE: NO enable_sleep_mode. The pool uses destroy-recreate (full
            # teardown) for handoffs, not vLLM-sleep, because two concurrent
            # CuMem (sleep-mode) engines conflict on Blackwell. Only one engine
            # is ever resident, so CuMem sleep buys nothing here.
        )
        if config.quantization:
            kwargs["quantization"] = config.quantization
        return LLM(**kwargs)

    @staticmethod
    def _strip_thinking(raw: str) -> str:
        """Remove reasoning preambles, returning only the final answer.

        Handles three formats observed across our specialists:
          <think>...</think>         — Qwen3, DeepSeek-R1
          <thinking>...</thinking>   — Qwen3 variants
          <|channel>thought ... <channel|> <answer>  — Gemma 4 AWQ harmony-style
            (delimiters render inconsistently: <|channel>, <channel|>, <|channel|>)
        """
        # 1. Gemma 4 channel format: the answer follows the LAST channel delimiter.
        if re.search(r"<\|?channel\|?>", raw):
            parts = re.split(r"<\|?channel\|?>", raw)
            raw = parts[-1]
            # Drop a leading channel label (thought/analysis/final/commentary).
            raw = re.sub(r"^\s*(thought|analysis|final|commentary)\b[:\s]*", "",
                         raw, flags=re.IGNORECASE)
            raw = raw.strip()

        # 2. XML-style think tags (closed or unclosed).
        for tag in ("thinking", "think"):
            if f"<{tag}>" in raw:
                raw = re.sub(rf"<{tag}>.*?</{tag}>", "", raw, flags=re.DOTALL).strip()
                raw = re.sub(rf"<{tag}>.*$", "", raw, flags=re.DOTALL).strip()

        # 3. Safety net: strip any residual harmony control tokens (message/start/
        #    end/return/channel) that survived — only these exact tokens, so we
        #    never clobber legitimate code containing angle brackets.
        raw = re.sub(r"<\|?(?:message|start|end|return|channel)\|?>", "", raw).strip()
        return raw

    @staticmethod
    def _build_messages(
        profile: "ModelProfile",
        user_text: str,
        screenshot_b64: Optional[str],
        context: Optional[str],
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": profile.system_prompt}]
        if context:
            messages.append({"role": "user",      "content": f"Context:\n{context}"})
            messages.append({"role": "assistant", "content": "Understood."})
        user_content: Any
        if screenshot_b64 and profile.supports_images:
            user_content = [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                {"type": "text", "text": user_text},
            ]
        else:
            user_content = user_text
        messages.append({"role": "user", "content": user_content})
        return messages

    async def _sleep_watchdog(self) -> None:
        """Periodically tear down specialists idle beyond the TTL to free VRAM.

        Destroy-recreate (not vLLM-sleep) — see _teardown_specialist.  Frees the
        full 16+ GB so the command engine has headroom; the specialist reloads on
        its next request.
        """
        while True:
            await asyncio.sleep(self._WATCHDOG_INTERVAL_S)
            now = time.monotonic()
            async with self._lock:
                for model_name, last in list(self._last_used.items()):
                    if (now - last > self._TTL_S and model_name in self._engines):
                        self._teardown_specialist(model_name)
                        self._last_used.pop(model_name, None)
                        log.info("VLLMSpecialistPool: TTL teardown %s (idle %.0fs)",
                                 model_name, now - last)

    def get_status(self) -> dict:
        return {
            "active":   self._active,
            "loaded":   list(self._engines),
            "sleeping": list(self._sleeping),
            "awake":    [m for m in self._engines if m not in self._sleeping],
        }


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouter:
    """Select the right model for a domain and run inference.

    Uses Ollama by default.  Call set_vllm_pool(pool) after construction to
    route all specialist domains through the vLLM INT4 pool instead.
    Ollama remains the fallback if the pool raises.
    """

    def __init__(
        self,
        host: str = "http://localhost:11434",
        timeout: float = 120.0,
    ) -> None:
        self._host = host
        self._timeout = timeout
        self._profiles = _PROFILES.copy()
        self._vllm_pool: Optional[VLLMSpecialistPool] = None
        # Single source of VRAM admission policy (gap B). Owns the tolerance that
        # was previously the inline `+ 2.0` magic number in select_profile.
        from core.vram_arbiter import VramArbiter
        self._arbiter = VramArbiter(tolerance_gb=2.0)
        # Cluster offload state (set via set_cluster()).
        self._cluster_config = None          # ClusterConfig | None
        self._cluster_health = None          # ClusterHealthMonitor | None
        self._lightweight_profile: Optional[ModelProfile] = None

    def set_vllm_pool(self, pool: VLLMSpecialistPool) -> None:
        """Wire the specialist pool.  Once set, all domain infer() calls use it."""
        self._vllm_pool = pool
        log.info("ModelRouter: vLLM specialist pool wired")

    def set_cluster(self, config, health_monitor=None) -> None:
        """Enable laptop offload of the command (lightweight) domain.

        config:         core.cluster_config.ClusterConfig
        health_monitor: core.cluster_health.ClusterHealthMonitor (optional). When
                        provided, offload only happens while the laptop Ollama is
                        healthy; otherwise inference stays local.
        """
        self._cluster_config = config
        self._cluster_health = health_monitor
        if config is not None and getattr(config, "offload_lightweight", False):
            base = self._profiles["lightweight"]
            # Build a host-bound copy so we never mutate the shared module profile.
            self._lightweight_profile = replace(base, inference_host=config.laptop_ollama_url)
            log.info(
                "ModelRouter: command-domain offload ENABLED → %s (%s)",
                self._lightweight_profile.name, config.laptop_ollama_url,
            )
        else:
            self._lightweight_profile = None
            log.info("ModelRouter: command-domain offload disabled (local only)")

    def heavy_model_names(self, min_vram_gb: float = 12.0) -> list[str]:
        """Distinct heavy specialist model names this router can load.

        Used by ResourceGovernor to know which Ollama models to evict on a flare,
        instead of hardcoding a single (easily-stale) name. Combines two sources:
          - profiles whose declared vram_gb >= min_vram_gb
          - fallback-chain entries that aren't in _LIGHT_MODELS (fallback names
            carry no vram metadata, so we treat any non-light fallback as heavy)
        Returns a stable, de-duplicated, sorted list.
        """
        heavy: set[str] = set()
        for profile in self._profiles.values():
            if profile.vram_gb >= min_vram_gb and profile.name not in _LIGHT_MODELS:
                heavy.add(profile.name)
        for chain in _FALLBACK.values():
            for name in chain:
                if name not in _LIGHT_MODELS:
                    heavy.add(name)
        return sorted(heavy)

    async def sleep_specialists(self) -> None:
        """Tear down any GPU-resident vLLM specialist (no-op on the Ollama path).

        Reuses VLLMSpecialistPool.sleep_all_specialists(). Safe to call when no
        pool is wired or when none are loaded.
        """
        if self._vllm_pool is None:
            return
        try:
            await self._vllm_pool.sleep_all_specialists()
        except Exception as exc:
            log.warning("ModelRouter.sleep_specialists failed: %s", exc)

    def _should_offload(self, domain: str) -> bool:
        """True when this domain's inference should run on the laptop node."""
        if self._lightweight_profile is None or domain != "command":
            return False
        if self._cluster_health is not None and not self._cluster_health.is_healthy("laptop_ollama"):
            return False
        return True

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
            if self._arbiter.can_admit(profile.vram_gb, free_gb):  # gap B: single admission policy
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

        Tries the vLLM pool first (if wired via set_vllm_pool); falls back to
        Ollama on any pool error so the agent never hard-fails.
        """
        # ── Cluster offload: lightweight command domain → laptop node ──────
        # Skips the desktop vLLM pool entirely (the pool is local). On any
        # remote error we fall through to the normal local path below.
        if self._should_offload(domain):
            remote = await self._infer_lightweight_remote(user_text, context, screenshot_b64)
            if remote is not None:
                return remote

        profile = self.select_profile(domain)
        t0 = time.monotonic()

        # ── vLLM specialist pool path ──────────────────────────────────────
        if self._vllm_pool is not None:
            try:
                response = await self._vllm_pool.infer(
                    model_name=profile.name,
                    profile=profile,
                    user_text=user_text,
                    screenshot_b64=screenshot_b64,
                    context=context,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                log.info("ModelRouter[vllm]: %s → %r (%.0f ms) [domain=%s]",
                         profile.name, response[:80], latency_ms, domain)
                try:
                    get_tracer().record_span("inference", route="dev", backend="vllm",
                                             model=profile.name, domain=domain,
                                             dur_ms=round(latency_ms, 1))
                except Exception:
                    pass
                return RouterResult(
                    text=response,
                    model=profile.name,
                    domain=domain,
                    latency_ms=latency_ms,
                    free_form=profile.free_form,
                    backend="vllm",
                )
            except Exception as exc:
                log.warning("ModelRouter: vLLM pool failed (%s), falling back to Ollama: %s",
                            profile.name, exc)
                t0 = time.monotonic()  # reset timer for fallback

        # ── Ollama fallback path ───────────────────────────────────────────
        prompt_parts = [profile.system_prompt]
        if context:
            prompt_parts.append(f"\nRecent context:\n{context}")
        prompt_parts.append(f"\nUser: {user_text}\nAssistant:")
        prompt = "\n".join(prompt_parts)

        try:
            response = await asyncio.to_thread(
                self._call_ollama, profile, prompt, screenshot_b64,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("ModelRouter[ollama]: %s → %r (%.0f ms) [domain=%s]",
                     profile.name, response[:80], latency_ms, domain)
            try:
                get_tracer().record_span("inference", route="dev", backend="ollama",
                                         model=profile.name, domain=domain,
                                         dur_ms=round(latency_ms, 1))
            except Exception:
                pass
            return RouterResult(
                text=response,
                model=profile.name,
                domain=domain,
                latency_ms=latency_ms,
                free_form=profile.free_form,
                backend="ollama",
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

    async def _infer_lightweight_remote(
        self,
        user_text: str,
        context: Optional[str],
        screenshot_b64: Optional[str],
    ) -> "Optional[RouterResult]":
        """Run the command-domain prompt on the laptop's Ollama.

        Returns a RouterResult on success, or None on any remote failure so the
        caller can fall back to the local path. A failed offload also pessimises
        the health monitor's view via the next poll (we don't mutate it here).
        """
        profile = self._lightweight_profile
        if profile is None:
            return None

        prompt_parts = [profile.system_prompt]
        if context:
            prompt_parts.append(f"\nRecent context:\n{context}")
        prompt_parts.append(f"\nUser: {user_text}\nAssistant:")
        prompt = "\n".join(prompt_parts)

        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self._call_ollama, profile, prompt, screenshot_b64,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("ModelRouter[laptop]: %s → %r (%.0f ms) [domain=command]",
                     profile.name, response[:80], latency_ms)
            return RouterResult(
                text=response,
                model=profile.name,
                domain="command",
                latency_ms=latency_ms,
                free_form=profile.free_form,
                backend="ollama-laptop",
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            log.warning(
                "ModelRouter: laptop offload failed (%s) — falling back to local: %s",
                profile.inference_host, exc,
            )
            return None
        except Exception as exc:  # unexpected — still fall back, don't hard-fail
            log.warning("ModelRouter: laptop offload error (%s) — local fallback", exc)
            return None

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
        # Structured output: constrain the response to a JSON Schema (Ollama
        # 0.5+). Only the plan profile sets this; the constraint applies after
        # any thinking block, so strip_thinking still cleans the response.
        if profile.json_schema:
            payload["format"] = profile.json_schema
        if screenshot_b64 and profile.supports_images:
            payload["images"] = [screenshot_b64]

        body = json.dumps(payload).encode()
        base_url = profile.inference_host or self._host
        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())

        raw = data.get("response", "").strip()

        # Strip thinking-tag blocks when profile.strip_thinking is True.
        # Keep for math — the chain-of-thought IS the answer.
        # Handles <think> (Qwen3/DeepSeek) and <thinking> (Gemma 4).
        if profile.strip_thinking:
            for tag in ("thinking", "think"):
                if f"<{tag}>" in raw:
                    raw = re.sub(rf"<{tag}>.*?</{tag}>", "", raw, flags=re.DOTALL).strip()
                    raw = re.sub(rf"<{tag}>.*$", "", raw, flags=re.DOTALL).strip()

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
            if self._arbiter.can_admit(p.vram_gb, free)
        }
        status: dict = {
            "free_vram_gb": round(free, 1),
            "available_specialists": available,
            "backend": "vllm" if self._vllm_pool is not None else "ollama",
            "vram_arbiter": self._arbiter.get_status(),
        }
        if self._vllm_pool is not None:
            status["vllm_pool"] = self._vllm_pool.get_status()
        return status


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
    backend: str = "ollama"   # "vllm" or "ollama"

    @property
    def ok(self) -> bool:
        return self.error is None

    def first_line(self) -> str:
        lines = [l.strip() for l in self.text.splitlines() if l.strip()]
        return lines[0] if lines else ""
