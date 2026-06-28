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

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# E2 — learned per-domain keyword overlay. OFF by default: with DA_DOMAIN_LEARN
# unset the classifier is exactly the static-keyword classifier (zero behaviour
# change, the router_domains eval baseline holds). When on, a small bounded nudge
# (capped at _MAX_OVERLAY_NUDGE) is added per domain from learned vocabulary — it
# can break ties but never override the static scores (command boost 40, skill 30+).
_DOMAIN_LEARN = os.environ.get("DA_DOMAIN_LEARN", "0").strip().lower() in (
    "1", "true", "on", "yes",
)

# Explicit "plan" word trigger (specs/cloud-plan-routing R5). When ON, a literal
# "plan"/"plans"/"planning" token anywhere in the query forces the plan domain to
# win — so the agentic plan_and_run loop (→ CloudPlanRouter → Sonnet 4.6) fires
# reliably without keyword-tuning the wording. Bypasses the _MIN_WORDS_FOR_DEV gate
# so a short "plan X" still routes to plan. Default OFF → byte-identical to the
# static classifier (the router_domains eval baseline holds). Command-bypass
# sources (touch / voice-click) never reach the classifier, so the accessibility
# path is unaffected. Enabled on this machine via DA_PLAN_WORD_TRIGGER=1.
_PLAN_WORD_TRIGGER = os.environ.get("DA_PLAN_WORD_TRIGGER", "0").strip().lower() in (
    "1", "true", "on", "yes",
)
_PLAN_TRIGGER_WORDS = frozenset({"plan", "plans", "planning"})
# Winning score: above the command short-verb boost (40) and any keyword score.
_PLAN_TRIGGER_SCORE = 60.0


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


# Verbs that take a proper-noun target (window / app) which ASR commonly
# capitalises and runs together with the verb ("OpenVSCode", "CloseSlack").
# Restricted to launch/window verbs — typing/text verbs are never glued to a
# capitalised target in practice, so they are excluded to avoid false splits.
_DEGLUE_VERBS = (
    "open", "close", "click", "focus", "switch", "maximize", "minimize",
)


def deglue_command_verb(text: str) -> str:
    """De-glue a leading command verb that ASR ran into its target word.

    Whisper sometimes transcribes "open VS Code" as the single token
    "OpenVSCode" (no space). With the verb hidden inside one token the
    DomainClassifier can no longer see a leading command verb, so the query
    falls through to the dev/general pipeline and never produces an OPEN action.

    This restores the space when — and only when — the first word starts with a
    known launch verb immediately followed by a camelCase boundary
    ("Open|VSCode"). The uppercase-boundary requirement keeps legitimate single
    tokens ("opener", "screenshot", a bare "open") untouched. Only the FIRST
    word is ever modified; whitespace and the rest of the utterance are
    preserved verbatim.
    """
    stripped = text.lstrip()
    if not stripped:
        return text
    lead = re.split(r"\s", stripped, maxsplit=1)[0]
    low = lead.lower()
    for verb in _DEGLUE_VERBS:
        if low == verb:
            return text  # already a standalone verb — nothing to do
    for verb in _DEGLUE_VERBS:
        if (
            low.startswith(verb)
            and len(lead) > len(verb)
            and lead[len(verb)].isupper()
        ):
            remainder = lead[len(verb):]
            # Skip pure acronyms ("OpenAPI", "OpenAI") — those are single proper
            # nouns, not a verb glued to an app name (which carries lowercase,
            # e.g. "VSCode", "Slack", "Notepad").
            if remainder.isupper():
                return text
            leading_ws = text[: len(text) - len(stripped)]
            rest = stripped[len(lead):]
            return f"{leading_ws}{lead[:len(verb)]} {remainder}{rest}"
    return text


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

    DOMAINS = ("command", "code", "math", "vision", "plan", "general", "skill")

    # Minimum query length (words) before considering non-command domains
    _MIN_WORDS_FOR_DEV = 4

    # Skill intent keywords, populated at startup by SkillRegistry. Class-level
    # so the coordinator's and DevAgent's classifiers share ONE vocabulary.
    # Empty → the skill domain never scores (no regression when no skills load).
    _SKILL_KEYWORDS: set[str] = set()

    @classmethod
    def register_skill_keywords(cls, keywords) -> None:
        """Replace the shared skill-intent keyword vocabulary (called once by
        SkillRegistry.start() with the union of all manifest intent phrases)."""
        cls._SKILL_KEYWORDS = {k.lower() for k in (keywords or ())}

    # E2 — learned per-domain keyword overlay {domain: {keyword: weight}}. Class
    # level so coordinator + DevAgent classifiers share ONE overlay. Applied only
    # when DA_DOMAIN_LEARN is on; empty → no nudge.
    _KEYWORD_OVERLAY: "dict[str, dict[str, float]]" = {}
    _MAX_OVERLAY_NUDGE = 15.0

    @classmethod
    def register_keyword_overlay(cls, overlay) -> None:
        """Install the learned per-domain keyword overlay (ContinuousTrainer loads
        it from AgentDB.get_domain_keyword_weights). Replaces any prior overlay."""
        cls._KEYWORD_OVERLAY = {
            d: {k.lower(): float(w) for k, w in (kw or {}).items()}
            for d, kw in (overlay or {}).items()
        }

    def _overlay_nudge(self, domain: str, tokens: "list[str]") -> float:
        """Bounded additive nudge for a domain from the learned overlay (0 when off)."""
        if not _DOMAIN_LEARN:
            return 0.0
        weights = self._KEYWORD_OVERLAY.get(domain)
        if not weights:
            return 0.0
        total = sum(weights.get(t, 0.0) for t in tokens)
        return min(self._MAX_OVERLAY_NUDGE, total)

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

        # Skill domain — substring match against manifest intent phrases (which
        # may be multi-word, beyond what the bigram tokenizer represents). Scored
        # for queries of ANY length; a matched intent phrase gets a strong score
        # so it beats an incidental dev/vision keyword overlap.
        text_l = text.lower()
        skill_matched = [kw for kw in self._SKILL_KEYWORDS if kw in text_l]
        skill_score = (30.0 + 5.0 * len(skill_matched)) if skill_matched else 0.0
        scores.append(DomainScore("skill", skill_score, skill_matched))

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

        # E2 — apply the bounded learned overlay nudge (no-op when DA_DOMAIN_LEARN
        # is off or the overlay is empty). Tie-breaker only: capped per domain.
        if _DOMAIN_LEARN and self._KEYWORD_OVERLAY:
            for ds in scores:
                ds.score += self._overlay_nudge(ds.domain, tokens)

        # Explicit "plan" word trigger (default OFF). A literal plan token forces
        # the plan domain to win, even when the _MIN_WORDS_FOR_DEV gate skipped the
        # dev-domain scoring above (so a short "plan X" still gets a plan entry).
        if _PLAN_WORD_TRIGGER and any(w in tokens for w in _PLAN_TRIGGER_WORDS):
            existing = next((s for s in scores if s.domain == "plan"), None)
            if existing is not None:
                existing.score = max(existing.score, _PLAN_TRIGGER_SCORE)
                if "plan" not in existing.matched_keywords:
                    existing.matched_keywords.insert(0, "plan")
            else:
                scores.append(DomainScore("plan", _PLAN_TRIGGER_SCORE, ["plan"]))

        scores.sort(key=lambda s: s.score, reverse=True)
        top = scores[0]
        runner_up = scores[1] if len(scores) > 1 else None
        log.debug(
            "domain_classifier: %r → %s (%.1f)%s",
            text[:60],
            top.domain,
            top.score,
            f"  runner-up={runner_up.domain}({runner_up.score:.1f})" if runner_up and runner_up.score > 1.0 else "",
        )
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
