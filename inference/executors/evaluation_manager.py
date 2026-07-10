import asyncio
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from core.approval_keywords import classify_confirmation
from core.domain_classifier import DomainClassifier
from core.events import (
    TOPIC_PLAN_GENERATED, TOPIC_DAG_STEP_STARTED, TOPIC_DAG_STEP_DONE,
    TOPIC_CHAT_TOKEN, TOPIC_DAG_APPROVAL,
    TOPIC_DAG_WALKTHROUGH, TOPIC_GOAL_DEQUEUED, TOPIC_GOAL_COMPLETED,
)
from inference.edit_format import (
    HASHLINE,
    HASHLINE_PROMPT_INSTRUCTIONS,
    SEARCH_REPLACE_PROMPT_INSTRUCTIONS,
    UDIFF,
    UDIFF_PROMPT_INSTRUCTIONS,
    EditApplier,
)
from inference.critic import PASS, REVISE, Critic, CriticVerdict, Finding
from inference.tester import Tester, is_testable_source
from inference.plan_parser import (
    AgentResult, AgentStep, _parse_plan, _parse_deps, _parse_plan_json, 
    _parse_plan_json_report, _build_plan_repair_prompt, _DELEGATE_PROMPT_INSTRUCTIONS,
    _PLAN_ACTIONS, _extract_json_obj, _STEP_PATTERN
)
from inference.model_router import ModelRouter, RouterResult


log = logging.getLogger(__name__)


# Constants missing
_DELEGATE_PROMPT_INSTRUCTIONS = ''


def _tester_should_skip(agent) -> bool:
    """Best-effort flare/resource gate (R3.6). Defaults to never-skip; a wired
    check that itself errors fails safe to SKIP (test-gen is non-essential)."""
    if agent._tester_skip_check is None:
        return False
    try:
        return bool(agent._tester_skip_check())
    except Exception:
        return True

async def _maybe_run_tester(agent, step: AgentStep, write_result: str) -> str:
    """After a committed WRITE_FILE, optionally generate + run a test and append
    its outcome to the step result as an observation (specs/dev-agent-critic R3).

    Default OFF → returns `write_result` unchanged. Only fires for `.py` source
    files; never raises, never blocks, never reports a skip as a pass.
    """
    if agent._tester is None or not agent._tester_enabled:
        return write_result
    target = (step.args or "").strip()
    if not is_testable_source(target):
        return write_result
    if _tester_should_skip(agent):
        log.info("DevAgent: tester skipped (flare/resource) — %s", target)
        return write_result
    try:
        code = await asyncio.to_thread(_read_current_for_critic, target)
        outcome = await agent._tester.generate_and_run(
            goal=agent._current_goal or "", path=target, code=code)
    except Exception as exc:
        log.warning("DevAgent: tester failed (%s) — skipped", exc)
        return write_result
    if outcome.note:
        return f"{write_result}\n{outcome.note}"
    return write_result

async def _critic_review(agent, step: AgentStep, new_text: str) -> CriticVerdict:
    """Independent review of a lint-passed WRITE_FILE edit (specs/dev-agent-critic).

    Default OFF → an immediate PASS (no model call), so the WRITE_FILE path is
    byte-identical to legacy. When ON: reviews the diff on the already-loaded
    model with a fresh reviewer context; fail-safe on any error (escalate to
    an explicit confirm, never a silent auto-approve — R1.5); bounds
    Critic-driven revise cycles per path (R1.7); sets `escalate` for a
    low-confidence PASS (R2.2).
    """
    if agent._critic is None or not agent._critic_enabled:
        return CriticVerdict(decision=PASS, confidence=1.0, escalate=False)

    target = (step.args or "").strip()
    try:
        current = await asyncio.to_thread(_read_current_for_critic, target)
        verdict = await agent._critic.review(
            goal=agent._current_goal or "", path=target,
            old_text=current, new_text=new_text,
        )
    except Exception as exc:
        log.warning("DevAgent: critic review failed (%s) — escalate to confirm "
                    "(fail-safe)", exc)
        return CriticVerdict(decision=PASS, confidence=0.0, escalate=True,
                             findings=[Finding("info", f"critic error: {exc}", target)])

    # R1.7 — bound revise cycles per path; once exhausted hand to the normal
    # flow (escalate + allow) so the step can't be revised forever.
    if verdict.decision == REVISE:
        n = agent._critic_revise_counts.get(target, 0) + 1
        agent._critic_revise_counts[target] = n
        if n > agent._critic_max_revisions:
            log.info("DevAgent: critic revise budget exhausted for %s — "
                     "escalate+allow", target)
            verdict.decision = PASS
            verdict.escalate = True
            return verdict

    # R2.2 — a low-confidence PASS still requires an explicit confirm.
    if verdict.decision == PASS and verdict.confidence < agent._critic_confidence_floor:
        verdict.escalate = True
    return verdict

@staticmethod
def _read_current_for_critic(path_str: str) -> str:
    """Current on-disk text for the diff the Critic reviews ('' if new file)."""
    from pathlib import Path as _P
    p = _P(path_str.strip().strip("'\""))
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

@staticmethod
def _critic_reject_message(agent, step: AgentStep, verdict: CriticVerdict) -> str:
    """Diagnostic step result for a blocked/revise edit → drives _replan."""
    target = (step.args or "").strip()
    return (f"{step.action.upper()} to {target[:60]} {verdict.decision} by critic: "
            f"{verdict.summary()}"
            + (f" | suggested fix: {verdict.suggested_fix}" if verdict.suggested_fix else ""))

async def _reflect(
    agent, goal: str, steps: list[AgentStep], model: str
) -> Optional[str]:
    """Reflect on executed steps: summarise outcomes, flag failures.

    Sends a lightweight prompt that includes each step's action + result
    so the model can reason about what was actually accomplished.
    Returns the reflection text, or None on failure.
    """
    if not steps:
        return None

    # Build step summary — include full result for failed steps so the
    # model can diagnose; truncate successes to avoid prompt bloat. The
    # trajectory compactor (spec specs/trajectory-reduction/) reproduces this
    # 200/600 budget byte-for-byte when reduction is off, and abstracts older
    # steps when DA_TRAJECTORY_REDUCE is on.
    from inference.trajectory import render_trajectory, reduction_enabled, dedup_enabled
    traj_text, _ = render_trajectory(
        steps, style="reflect", success_chars=200, failure_chars=600,
        readonly_verbs=agent._PARALLEL_VERBS, enabled=reduction_enabled(),
        dedup_reads=dedup_enabled(),
    )
    lines = [f"Goal: {goal}", "", "Steps executed:", traj_text]

    lines += [
        "",
        "Briefly summarise: what was accomplished, what (if anything) failed,"
        " and what the user should know or do next.",
    ]
    reflect_prompt = "\n".join(lines)

    try:
        r = await agent._router.infer(
            domain="general",
            user_text=reflect_prompt,
            context=None,
        )
        if r.ok and r.text:
            log.info("DevAgent: reflection — %s", r.text[:120])
            return r.text
    except Exception as exc:
        log.debug("DevAgent._reflect() failed: %s", exc)
    return None

