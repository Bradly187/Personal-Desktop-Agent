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


def _delegate_should_skip_flare(agent) -> bool:
    """True if a flare is active (AGENTS.md #5) — investigation is non-essential
    heavy work. A skip-check that errors fails safe to SKIP."""
    if agent._delegate_skip_check is None:
        return False
    try:
        return bool(agent._delegate_skip_check())
    except Exception:
        return True

async def _delegate_investigate(agent, question: str, depth: int) -> str:
    """Run a bounded, READ-ONLY investigation sub-agent and return its finding.

    Reuses the WorkflowRunner substrate (scheduler sub-agent pool, flare guard,
    agent_workflows journaling); the child runs a small plan→execute loop
    restricted to read-only verbs (never re-entering plan_and_run / _plan_lock,
    R3.2). Always returns a safe observation string — never raises into the
    parent's _execute_step (R4.3).
    """
    if not agent._delegate_enabled:
        return "DELEGATE skipped: feature disabled"
    if not question:
        return "DELEGATE skipped: empty question"
    if depth > agent._max_delegate_depth:        # no recursion / fan-bomb (R3.1)
        return "DELEGATE refused: max delegation depth"
    if _delegate_should_skip_flare(agent):      # AGENTS.md #5 (R4.2)
        await _journal_delegate(agent, question, 0, 0, "skipped_flare")
        return "DELEGATE skipped: flare"

    async def _run() -> str:
        return await _delegate_loop(agent, question, depth)

    try:
        if agent._scheduler is not None and hasattr(agent._scheduler, "fan_out"):
            # Run under the sub-agent semaphore, not the dev permit (R3.2).
            results = await agent._scheduler.fan_out([_run()], label="delegate")
            r = results[0] if results else None
            if isinstance(r, BaseException) or r is None:
                raise r if isinstance(r, BaseException) else RuntimeError("no result")
            return r
        return await _run()
    except Exception as exc:                    # safe observation, never raise (R4.3)
        log.warning("DevAgent._delegate_investigate(%r) failed: %s", question[:60], exc)
        await _journal_delegate(agent, question, 0, 0, "error", error=str(exc))
        return f"DELEGATE failed: {exc}"

async def _delegate_loop(agent, question: str, depth: int) -> str:
    """The bounded read-only investigation itself (no _plan_lock). Plan →
    execute read-only steps (allowlist-enforced) → synthesize a finding."""
    # Scoped context: the question + any RAG hits for it. The child inherits
    # *enough to help*, not the parent's full trajectory (bounded payload).
    rag = await agent._rag_context(question, n=3)
    child_ctx = (
        "You are a READ-ONLY investigator. Answer the question using ONLY these "
        "verbs: READ_FILE, GREP, FETCH_URL, READ_SCREEN, GIT_STATUS, GIT_DIFF, "
        "SEARCH_PERSONAL. You may NOT write files, run shell, or take any action. "
        f"Produce at most {agent._delegate_max_steps} steps in the [ACTION args] "
        "format, then stop."
    )
    if rag:
        child_ctx = f"{rag}\n\n{child_ctx}"

    plan_result = await agent._router.infer(
        domain="plan", user_text=f"Investigate: {question}", context=child_ctx,
    )
    steps: list[AgentStep] = []
    if getattr(plan_result, "ok", True):
        try:
            steps = _parse_plan_json(plan_result.text)
        except Exception:
            steps = []                      # not structured JSON — fall back
        if not steps:
            steps = _parse_plan(plan_result.text)
    steps = steps[: agent._delegate_max_steps]

    observations: list[str] = []
    ran = 0
    prev_depth = agent._delegate_depth
    agent._delegate_depth = depth        # so a nested DELEGATE is refused (R3.1)
    try:
        for s in steps:
            act = s.action.upper()
            if act not in agent._PARALLEL_VERBS:
                # Deny-by-default: a child step naming any non-read-only verb is
                # DROPPED, never executed (R2.1). Structurally read-only.
                log.info("DevAgent.delegate: dropped non-read-only child step %s", act)
                continue
            try:
                res = await asyncio.wait_for(
                    agent._execute_step(s), timeout=agent.STEP_TIMEOUT_S)
                observations.append(f"[{act} {s.args[:60]}]\n{(res or '')[:600]}")
                ran += 1
            except Exception as exc:
                observations.append(f"[{act} {s.args[:60]}] failed: {exc}")
    finally:
        agent._delegate_depth = prev_depth

    if not observations:
        await _journal_delegate(agent, question, len(steps), 0, "completed")
        return f"DELEGATE finding: no read-only evidence gathered for: {question}"

    synth = await agent._router.infer(
        domain="plan",
        user_text=(
            f"Question: {question}\n\nRead-only observations:\n"
            + "\n\n".join(observations)
            + "\n\nAnswer the question concisely from the observations only."
        ),
        context="",
    )
    finding = (getattr(synth, "text", "") or "").strip()[: agent._delegate_finding_chars]
    await _journal_delegate(agent, question, len(steps), ran, "completed")
    return f"DELEGATE finding: {finding}" if finding else \
        f"DELEGATE finding: gathered {ran} observation(s) for: {question}"

async def _journal_delegate(
    agent, question: str, subtask_count: int, success_count: int,
    status: str, error: Optional[str] = None,
) -> None:
    """Best-effort journal to the existing agent_workflows ledger (mode=
    'delegate', R4.1) — a DB failure never breaks the investigation."""
    db = agent._agent_db
    if db is None or not getattr(db, "available", True):
        return
    try:
        await db.workflows.insert_workflow(
            name=f"delegate:{question[:40]}", goal=question, mode="delegate",
            subtask_count=subtask_count, success_count=success_count,
            status=status, error=error,
        )
    except Exception as exc:
        log.debug("DevAgent._journal_delegate failed: %s", exc)

