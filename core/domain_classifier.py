"""DomainClassifier — keyword-scoring domain detection for the dev agent pipeline.

Classifies a natural-language query into one of six domains, which determines
which specialist model and prompt strategy the ModelRouter will select.

Domains:
  command  — simple one-shot desktop action (click, scroll, hotkey, etc.)
  code     — write, explain, or debug code; ML/QC frameworks; software dev
  math     — mathematical reasoning, proofs, derivations, theory
  vision   — analyse what's on screen; read a diagram/paper; OCR + reasoning
  plan     — multi-step project task that needs decomposition before execution
  general  — everything else (explanation, research synthesis, writing)

Classification is purely keyword-based — no additional model call needed.
Scoring weights multiple evidence signals so domain boundaries are soft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Keyword sets per domain
# ---------------------------------------------------------------------------

_COMMAND_VERBS = {
    "click", "scroll", "open", "close", "type", "press", "copy", "paste",
    "save", "undo", "redo", "select", "focus", "minimize", "maximize",
    "screenshot", "dictate", "hotkey", "drag", "move", "resize", "switch",
    "tab", "window", "alt", "ctrl", "enter", "escape", "backspace",
}

_CODE_KEYWORDS = {
    # Languages & runtimes
    "python", "rust", "c++", "cpp", "javascript", "typescript", "julia",
    "cuda", "triton", "wgsl",
    # ML / AI frameworks
    "pytorch", "torch", "tensorflow", "jax", "flax", "keras", "huggingface",
    "transformers", "diffusers", "vllm", "llama", "mistral", "bert", "gpt",
    "langchain", "langgraph", "autogen", "crewai", "llamaindex",
    "sklearn", "scikit", "xgboost", "lightgbm", "catboost",
    # ML concepts that imply writing code
    "model", "train", "training", "inference", "fine-tune", "finetune",
    "dataset", "dataloader", "batch", "epoch", "gradient", "optimizer",
    "loss", "backprop", "autograd", "checkpoint", "embedding", "tokenizer",
    "attention", "transformer", "encoder", "decoder", "layer", "activation",
    # Quantum computing
    "qiskit", "pennylane", "cirq", "braket", "tket", "stim",
    "circuit", "gate", "qubit", "hadamard", "cnot", "pauli", "ansatz",
    "variational", "vqe", "qaoa", "grover", "shor", "quantum",
    # Dev tooling
    "function", "class", "def", "import", "module", "package", "api",
    "debug", "test", "unittest", "pytest", "mock", "fixture",
    "git", "commit", "branch", "merge", "pull", "push", "clone",
    "docker", "kubernetes", "container", "deploy", "pipeline",
    "async", "await", "coroutine", "thread", "process",
    "implement", "write", "code", "script", "refactor", "optimise", "optimize",
}

_MATH_KEYWORDS = {
    # Pure math
    "prove", "proof", "theorem", "lemma", "corollary", "conjecture",
    "derivation", "derive", "show that", "given that",
    "integral", "derivative", "differential", "calculus",
    "matrix", "vector", "tensor", "eigenvalue", "eigenvector", "determinant",
    "trace", "rank", "null space", "kernel", "span", "basis",
    "norm", "metric", "distance", "inner product", "dot product",
    "fourier", "laplace", "transform", "convolution",
    "probability", "distribution", "expectation", "variance", "covariance",
    "entropy", "kl divergence", "mutual information", "log likelihood",
    "bayes", "prior", "posterior", "marginal",
    "optimization", "convex", "concave", "saddle", "hessian", "jacobian",
    "lagrangian", "dual", "primal", "constraint",
    # Quantum math / physics
    "hilbert", "operator", "hamiltonian", "unitary", "hermitian",
    "superposition", "entanglement", "decoherence", "fidelity",
    "density matrix", "bloch sphere", "measurement",
    # General indicators
    "equation", "formula", "expression", "algebra", "topology",
    "manifold", "group", "ring", "field", "algebra",
    "compute", "calculate", "solve", "simplify", "expand",
}

_VISION_KEYWORDS = {
    "screen", "on screen", "screenshot", "display", "what is on",
    "what does it say", "read", "analyse", "analyze", "diagram",
    "figure", "chart", "plot", "graph", "image", "picture", "photo",
    "paper", "pdf", "article", "page", "look at", "see", "show",
    "describe what", "what am i looking at", "what is this",
    "caption", "ocr", "extract text",
}

_PLAN_KEYWORDS = {
    # Project creation
    "set up", "setup", "create a project", "new project", "scaffold",
    "build a", "make a", "implement a", "design a", "architect",
    "from scratch", "end to end", "full pipeline",
    # Multi-step markers
    "step by step", "walkthrough", "how do i", "how to",
    "first", "then", "after that", "followed by", "finally",
    "workflow", "pipeline", "process", "procedure",
    # Research / learning
    "research", "explore", "investigate", "survey", "compare",
    "reproduce", "replicate", "experiment",
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class DomainScore:
    domain: str
    score: float
    matched_keywords: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    """Lower-case, strip punctuation, return word tokens + bigrams."""
    text = text.lower()
    words = re.findall(r"[a-z0-9_\-]+", text)
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
    return words + bigrams


def _score_against(tokens: list[str], keyword_set: set[str]) -> tuple[float, list[str]]:
    matched = [t for t in tokens if t in keyword_set]
    # Unique matches weighted by set coverage (penalise accidental overlaps)
    unique = list(dict.fromkeys(matched))
    score = len(unique) / max(1, len(tokens)) * 100
    return score, unique


# ---------------------------------------------------------------------------
# DomainClassifier
# ---------------------------------------------------------------------------

class DomainClassifier:
    """Classify a query into one of six domains using keyword scoring."""

    DOMAINS = ("command", "code", "math", "vision", "plan", "general")

    # Minimum query length (words) before considering non-command domains
    _MIN_WORDS_FOR_DEV = 4

    def classify(self, text: str) -> str:
        """Return the most likely domain string."""
        return self.score(text)[0].domain

    def score(self, text: str) -> list[DomainScore]:
        """Return all domains sorted by score descending."""
        tokens = _tokenize(text)
        word_count = len(text.split())

        scores: list[DomainScore] = []

        # Command domain — prioritise very short queries with command verbs
        cmd_score, cmd_matched = _score_against(tokens, _COMMAND_VERBS)
        # Boost command score for short, verb-leading queries
        if word_count <= 5 and tokens and tokens[0] in _COMMAND_VERBS:
            cmd_score = max(cmd_score, 40.0)
        scores.append(DomainScore("command", cmd_score, cmd_matched))

        # Dev domains only make sense for longer queries
        if word_count >= self._MIN_WORDS_FOR_DEV:
            code_score, code_matched = _score_against(tokens, _CODE_KEYWORDS)
            math_score, math_matched = _score_against(tokens, _MATH_KEYWORDS)
            vision_score, vision_matched = _score_against(tokens, _VISION_KEYWORDS)
            plan_score, plan_matched = _score_against(tokens, _PLAN_KEYWORDS)

            # Vision bonus for explicit screen-reading intent
            if any(p in text.lower() for p in ("what's on", "what is on", "what am i")):
                vision_score += 20.0

            # Plan bonus when multiple step markers present
            step_markers = sum(1 for kw in ("then", "after", "first", "finally")
                               if kw in tokens)
            if step_markers >= 2:
                plan_score += step_markers * 5.0

            scores += [
                DomainScore("code",    code_score,    code_matched),
                DomainScore("math",    math_score,    math_matched),
                DomainScore("vision",  vision_score,  vision_matched),
                DomainScore("plan",    plan_score,    plan_matched),
            ]

        # General is the fallback — always present but lowest priority
        scores.append(DomainScore("general", 0.5, []))

        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    def explain(self, text: str) -> str:
        """Human-readable explanation of the classification decision."""
        results = self.score(text)
        top = results[0]
        lines = [f"Domain: {top.domain} (score={top.score:.1f})"]
        if top.matched_keywords:
            lines.append(f"Matched: {', '.join(top.matched_keywords[:8])}")
        for r in results[1:3]:
            if r.score > 1.0:
                lines.append(f"  runner-up: {r.domain} ({r.score:.1f})")
        return " | ".join(lines)
