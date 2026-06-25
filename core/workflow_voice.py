"""Voice trigger for multi-agent workflow orchestration.

``inference/workflow.WorkflowRunner`` fans a goal out to N fresh-context
sub-agents and (optionally) adversarially verifies each — but it is a *building
block*: it takes caller-supplied sub-tasks and runs no decomposition of its own.
This module is the **live voice trigger** that turns a spoken goal into that
fan-out: a deterministic phrase parser plus the pure prompt/parsing helpers the
orchestrator (``HybridCoordinator``) uses to decompose the goal, run the runner,
and synthesize one spoken answer.

The flow when the feature is ON (``workflow_orchestration.enabled``):

    "think hard about <goal>" / "research <goal>"
        → parse_workflow_request → goal
        → build_decompose_prompt → general model → parse_decomposition → N angles
        → WorkflowRunner.fan_out(angles)            (concurrent fresh contexts)
        → build_synthesis_prompt → general model → one concise answer
        → spoken back via TTS (with the mic-feedback suppress guard)

Design constraints honoured here (mirrors ``core/conversation_mode``):

* **Pure / synchronous / no I/O.** Every function in this module is
  deterministic and model-free — all inference, TTS, and mic suppression live in
  ``HybridCoordinator``. So it is trivial to unit-test and cheap to call on every
  command (AGENTS.md #2 — nothing heavy near the 60 Hz tick).
* **Fail-safe (AGENTS.md #4).** Detection is conservative anchored matching: a
  trigger phrase must lead the utterance and be followed by a non-empty goal, so
  ordinary speech never fires it. A non-match returns ``None`` and the utterance
  routes normally.
* **Default OFF.** Gated by the same ``workflow_orchestration.enabled`` flag as
  the runner — enabling orchestration enables its voice trigger. Byte-identical
  legacy when unset.

Spec: ``specs/workflow-orchestration/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "WorkflowRequest",
    "parse_workflow_request",
    "parse_decomposition",
    "build_decompose_prompt",
    "build_synthesis_prompt",
    "workflow_voice_config",
    "fanout_n_from_config",
    "verify_enabled",
    "DEFAULT_FANOUT_N",
]

# Number of sub-angles to fan out by default. Kept small: the single RTX-5090
# serializes inference at the Ollama layer regardless of scheduler concurrency,
# so each extra angle is roughly additive latency. Config may override within
# [1, _MAX_FANOUT_N].
DEFAULT_FANOUT_N = 3
_MAX_FANOUT_N = 6

# Leading politeness/filler stripped before matching, so "hey agent, could you
# research X" normalises to "research X". Deliberately small and trigger-local.
_LEADING_FILLER: frozenset[str] = frozenset(
    {"hey", "ok", "okay", "agent", "computer", "please", "um", "uh", "so",
     "can", "could", "would", "you", "please"}
)
_TRAILING_FILLER: frozenset[str] = frozenset(
    {"please", "now", "then", "for", "me", "thanks"}
)

# Lead-anchor trigger phrases. Each must START the (normalised) utterance and be
# followed by a non-empty goal. Chosen to be distinctive enough that they do not
# collide with ordinary accessibility commands ("open chrome", "scroll down").
_DEFAULT_TRIGGERS: tuple[str, ...] = (
    "think hard about",
    "think deeply about",
    "do some research on",
    "do research on",
    "research",
    "brainstorm ideas for",
    "brainstorm",
    "look at this from every angle",
    "explore from every angle",
    "weigh the pros and cons of",
)


def _normalize(text: str) -> str:
    """Lower-case, punctuation→space, collapse whitespace, drop a small set of
    leading/trailing filler words. Empty string for empty input."""
    if not text:
        return ""
    norm = re.sub(r"[^\w\s]", " ", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return ""
    words = norm.split()
    while words and words[0] in _LEADING_FILLER:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLER:
        words.pop()
    return " ".join(words)


@dataclass
class WorkflowRequest:
    """A parsed voice workflow request: the goal and the trigger that matched."""

    goal: str
    trigger: str


def parse_workflow_request(text: str) -> "WorkflowRequest | None":
    """Detect a workflow trigger leading ``text`` and extract the goal.

    Returns a :class:`WorkflowRequest` when the normalised utterance begins with
    a trigger phrase AND a non-empty goal follows it, else ``None``. Anchored at
    the START (longest phrase first) so "research quantum error correction"
    matches with goal "quantum error correction", while a bare "research" (no
    goal) and ordinary commands ("open chrome") do not. Pure / deterministic.
    """
    norm = _normalize(text)
    if not norm:
        return None
    tokens = norm.split()
    for phrase in sorted(_DEFAULT_TRIGGERS, key=lambda p: -len(p.split())):
        ptokens = phrase.split()
        if ptokens and tokens[: len(ptokens)] == ptokens:
            goal = " ".join(tokens[len(ptokens):]).strip()
            if goal:
                return WorkflowRequest(goal=goal, trigger=phrase)
            return None  # bare trigger, no goal — not a workflow request
    return None


_DECOMP_PREFIX = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)")


def parse_decomposition(text: str, n: int) -> list[str]:
    """Parse a decomposition model reply (one angle per line) into a clean list.

    Strips numbering / bullet prefixes, drops blank lines, de-duplicates while
    preserving order, and caps the result at ``n``. Pure / deterministic — used
    so the model's free-form output can never blow up the fan-out width or smuggle
    empties into the sub-task list.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = _DECOMP_PREFIX.sub("", raw).strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= max(1, n):
            break
    return out


def build_decompose_prompt(goal: str, n: int) -> str:
    """Prompt the general model to split ``goal`` into ``n`` focused angles."""
    return (
        f"Break the following goal into {n} distinct, focused sub-questions or "
        f"angles that together cover it well. Output exactly {n} lines — one "
        f"sub-question per line, no numbering, no preamble, no blank lines.\n\n"
        f"Goal: {goal}"
    )


def build_synthesis_prompt(goal: str, results: list[str]) -> str:
    """Prompt the general model to synthesize sub-answers into one spoken reply."""
    joined = "\n\n".join(
        f"Angle {i + 1}:\n{r.strip()}" for i, r in enumerate(results) if r.strip()
    )
    return (
        "You explored a question from several angles. Synthesize the findings "
        "into one clear, concise spoken answer — a few sentences at most, no "
        "markdown, no headings, no bullet lists, no emoji, suitable for being "
        f"read aloud by text-to-speech.\n\nQuestion: {goal}\n\n{joined}"
    )


def workflow_voice_config() -> dict:
    """The ``workflow_orchestration`` config block (same flag as the runner).

    Default: disabled (experimental — ship behind a flag, AGENTS.md #9)."""
    default = {"enabled": False}
    try:
        cfg_path = Path(os.path.expanduser("~/.claude/ipad_bridge/config.json"))
        if not cfg_path.exists():
            return default
        block = json.loads(cfg_path.read_text(encoding="utf-8")).get(
            "workflow_orchestration"
        )
        return block if isinstance(block, dict) else default
    except Exception as exc:  # malformed config must never crash the pipeline
        log.debug("workflow_orchestration config unreadable (%s)", exc)
        return default


def fanout_n_from_config(cfg: dict | None) -> int:
    """Fan-out width from config, clamped to [1, _MAX_FANOUT_N]. Default 3."""
    try:
        n = int((cfg or {}).get("fanout_n", DEFAULT_FANOUT_N))
    except (TypeError, ValueError):
        return DEFAULT_FANOUT_N
    return max(1, min(_MAX_FANOUT_N, n))


def verify_enabled(cfg: dict | None) -> bool:
    """Whether to run the adversarial verify pass on the voice path (default
    OFF — verify doubles inference cost and the user just wants an answer)."""
    return bool((cfg or {}).get("verify", False))
