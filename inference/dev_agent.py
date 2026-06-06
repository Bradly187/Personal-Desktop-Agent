"""DevAgent — agentic task loop for software development and research tasks.

Implements a plan → execute → observe → reflect loop that lets the agent
autonomously complete multi-step development tasks using specialist models
and an expanded action vocabulary.

Expanded action verbs (beyond the 9 accessibility verbs):
  WRITE_FILE <path>   — write content to a file
  RUN_TERMINAL <cmd>  — execute a shell command, capture output
  EXPLAIN <text>      — return a text response to the user (no desktop action)
  SEARCH_WEB <query>  — open browser with search query
  READ_SCREEN         — take screenshot, optionally pass to vision model

Entry points:
  DevAgent.handle(text)    — classify, route, execute; single-turn or agentic
  DevAgent.plan_and_run(goal)  — full plan→execute→reflect loop

The DevAgent sits above HybridCoordinator. Simple accessibility commands
(domain="command") are passed straight through to the existing pipeline.
All dev-domain queries are handled by specialist models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.approval_keywords import classify_confirmation
from core.domain_classifier import DomainClassifier
from inference.model_router import ModelRouter, RouterResult

if TYPE_CHECKING:
    from inference.codebase_indexer import CodebaseIndexer
    from core.command_executor import Command, CommandExecutor
    from adaptive.continuous_trainer import ContinuousTrainer
    from storage.db import AgentDB
    from core.hybrid_coordinator import HybridCoordinator
    from inference.kiro_client import KiroClient
    from mcp_server.tools import screen as screen_tools

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML text extraction helper
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Very simple HTML → plain text: strip tags, collapse whitespace."""
    # Remove script/style blocks entirely
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags
    clean = re.sub(r"<[^>]+>", " ", clean)
    # Collapse whitespace
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()

# ---------------------------------------------------------------------------
# Step model
# ---------------------------------------------------------------------------

# Action verbs the planner model is allowed to emit
_PLAN_ACTIONS = {
    "WRITE_FILE", "RUN_TERMINAL", "CLICK", "OPEN", "HOTKEY",
    "EXPLAIN", "SEARCH_WEB", "READ_SCREEN", "READ_FILE", "GREP",
    "SCROLL", "TYPE",
    # Git-native verbs (item #3 / #8 in roadmap)
    "GIT_STATUS", "GIT_DIFF", "GIT_COMMIT", "GIT_CHECKOUT",
    # GitHub integration
    "GITHUB_PR",
    # Web retrieval (replaces browser-open SEARCH_WEB for context injection)
    "FETCH_URL",
}

_STEP_PATTERN = re.compile(
    r"^\s*(?:Step\s*\d+[:.]\s*)?"          # optional "Step N:"
    r"\[?"                                   # optional [
    r"(WRITE_FILE|RUN_TERMINAL|CLICK|OPEN|HOTKEY|EXPLAIN|SEARCH_WEB"
    r"|READ_SCREEN|READ_FILE|GREP|SCROLL|TYPE"
    r"|GIT_STATUS|GIT_DIFF|GIT_COMMIT|GIT_CHECKOUT|GITHUB_PR|FETCH_URL)"
    r"(?:\s+([^\]\n]+))?"                   # optional args (up to a closing ] or EOL)
    r"\s*\]?",                              # optional ]
    re.IGNORECASE,
)


@dataclass
class AgentStep:
    action: str
    args: str = ""
    body: str = ""          # multi-line content (e.g. file content)
    result: Optional[str] = None
    success: Optional[bool] = None
    latency_ms: float = 0.0


@dataclass
class AgentResult:
    goal: str
    domain: str
    model_used: str
    steps: list[AgentStep] = field(default_factory=list)
    response_text: str = ""    # for single-turn (non-plan) responses
    success: bool = True
    error: Optional[str] = None
    total_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

def _parse_plan(text: str) -> list[AgentStep]:
    """Extract AgentStep objects from a planner model response."""
    steps: list[AgentStep] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _STEP_PATTERN.match(lines[i])
        if m:
            action = m.group(1).upper()
            args = (m.group(2) or "").strip()
            # Collect body lines until the next step or end
            body_lines = []
            i += 1
            while i < len(lines):
                if _STEP_PATTERN.match(lines[i]):
                    break
                body_lines.append(lines[i])
                i += 1
            # Strip leading/trailing blank lines from body
            body = "\n".join(body_lines).strip()
            # Remove markdown code fences from body
            body = re.sub(r"^```[a-z]*\n?", "", body, flags=re.MULTILINE)
            body = re.sub(r"\n?```$", "", body, flags=re.MULTILINE).strip()
            steps.append(AgentStep(action=action, args=args, body=body))
        else:
            i += 1
    return steps


# ---------------------------------------------------------------------------
# DevAgent
# ---------------------------------------------------------------------------

class DevAgent:
    """Agentic loop for dev-domain tasks.

    Wire-up in main.py:
        dev_agent = DevAgent(router, coordinator, trainer)
        result = await dev_agent.handle("write a PyTorch CNN for CIFAR-10")
    """

    # How many steps we'll run autonomously before pausing
    MAX_STEPS = 20

    # How many times the controller may revise the plan after a step failure
    # before giving up (bounds the observe→act→replan loop).
    MAX_REPLANS = 2

    # Read-only / idempotent verbs that are safe to retry once on a transient
    # failure. Destructive verbs are NEVER retried (re-running a commit / write /
    # shell command could double-apply) — they go straight to replan-or-halt.
    _RETRYABLE_VERBS: frozenset[str] = frozenset({
        "READ_FILE", "GREP", "READ_SCREEN", "GIT_STATUS", "GIT_DIFF",
        "FETCH_URL", "SEARCH_WEB", "EXPLAIN",
    })

    # Verbs whose execution has side effects (writes files, runs shell, mutates
    # git, opens a PR). A plan containing ANY of these is "destructive": its
    # upfront approval, and any per-op confirmation, must fail-safe to DENY on
    # silence / ambiguity / hardware failure — mirroring the hardened voice
    # approval gate (approval_hook.py, timeout_action="reject"). Read-only plans
    # keep their convenient auto-approve-on-silence.
    _DESTRUCTIVE_VERBS: frozenset[str] = frozenset({
        "WRITE_FILE", "RUN_TERMINAL", "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR",
    })

    # Pure context-gathering verbs (gap #1 fan-out): read-only, no side effects,
    # and no inter-step dependency worth serialising. A LEADING run of these in a
    # plan can be executed concurrently via the scheduler's sub-agent pool before
    # the sequential action loop. Subset of _RETRYABLE_VERBS (excludes SEARCH_WEB,
    # which opens a browser, and EXPLAIN, which is ordering-sensitive narration).
    _PARALLEL_VERBS: frozenset[str] = frozenset({
        "READ_FILE", "GREP", "FETCH_URL", "READ_SCREEN", "GIT_STATUS", "GIT_DIFF",
    })

    def __init__(
        self,
        router: ModelRouter,
        coordinator: Optional["HybridCoordinator"] = None,
        trainer: Optional["ContinuousTrainer"] = None,
        session_context: Optional[list[str]] = None,
        agent_db: Optional["AgentDB"] = None,
    ) -> None:
        self._router = router
        self._coordinator = coordinator
        self._trainer = trainer
        self._agent_db = agent_db
        self._classifier = DomainClassifier()
        self._context: list[str] = session_context or []
        self._results_log: list[AgentResult] = []  # kept for get_last_result()
        self._indexer: Optional["CodebaseIndexer"] = None   # set via set_indexer()
        self._kiro: Optional["KiroClient"] = None            # set via set_kiro()
        self._scheduler = None                               # set via set_scheduler()
        self._memory = None                                  # set via set_memory()
        self._remote_indexer = None       # RemoteIndexerClient | None (laptop offload)
        self._cluster_health = None        # ClusterHealthMonitor | None
        self._confirm_whisper = None       # WhisperModel cached for _confirm_destructive_op()

        # Goal-level authorization state (reset after each plan)
        self._plan_authorized: bool = False
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._current_goal: Optional[str] = None
        self._current_step: int = 0
        self._total_steps: int = 0

    def set_indexer(self, indexer: "CodebaseIndexer") -> None:
        """Wire a CodebaseIndexer for RAG context injection at plan/query time."""
        self._indexer = indexer

    def set_remote_indexer_url(self, url: str) -> None:
        """Offload RAG queries to the laptop indexer service at `url`.

        Preferred over the local indexer when the laptop 'indexer' service is
        healthy; falls back to the local indexer otherwise.
        """
        from inference.remote_indexer_client import RemoteIndexerClient
        self._remote_indexer = RemoteIndexerClient(url)
        log.info("DevAgent: remote indexer enabled → %s", url)

    def set_cluster_health(self, monitor) -> None:
        """Wire ClusterHealthMonitor; remote indexer used only while 'indexer' is healthy."""
        self._cluster_health = monitor

    def set_kiro(self, kiro: "KiroClient") -> None:
        """Wire a KiroClient for IDE context (cursor, file, git, diagnostics)."""
        self._kiro = kiro

    def set_scheduler(self, scheduler) -> None:
        """Wire AccessibilityScheduler for submitting background sub-tasks at DEV_AGENT priority."""
        self._scheduler = scheduler

    def set_memory(self, memory) -> None:
        """Wire MemoryManager for standardised storage access."""
        self._memory = memory

    def request_cancel(self) -> None:
        """Signal the running plan to stop after the current step completes."""
        self._cancel_event.set()
        log.info("DevAgent: cancel requested — will stop after current step")

    def get_plan_status(self) -> dict:
        """Return the current plan execution state for voice status queries."""
        if self._current_goal is None:
            return {"active": False}
        return {
            "active": True,
            "goal": self._current_goal,
            "step": self._current_step,
            "total_steps": self._total_steps,
            "authorized": self._plan_authorized,
            "cancel_requested": self._cancel_event.is_set(),
        }

    # ---------------------------------------------------------------------- #
    # Primary entry point
    # ---------------------------------------------------------------------- #

    async def handle(self, text: str, screenshot_b64: Optional[str] = None) -> AgentResult:
        """Classify, route, and execute a user query.

        - COMMAND domain → passes through to HybridCoordinator (existing pipeline)
        - PLAN domain → plan_and_run loop
        - CODE/MATH/VISION/GENERAL → single specialist inference, result returned
        """
        t0 = time.monotonic()
        domain = self._classifier.classify(text)
        log.info("DevAgent: domain=%s  text=%r", domain, text[:80])

        if domain == "command" and self._coordinator:
            # Pass through to the accessibility pipeline
            from core.command_executor import Command
            cmd = Command(text=text, action="CLARIFY", source="voice")
            result_dict = await self._coordinator.route(cmd)
            return AgentResult(
                goal=text,
                domain="command",
                model_used="llama3.1:8b",
                response_text=str(result_dict),
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        if domain == "plan":
            return await self.plan_and_run(text)

        if domain == "vision" and screenshot_b64 is None:
            # Auto-capture screen for vision queries
            screenshot_b64 = await self._capture_screenshot()

        # Single-turn specialist inference — inject RAG context for dev domains
        extra_ctx = self._format_context()
        if domain in ("code", "math", "vision", "general", "plan"):
            rag = await self._rag_context(text, n=3)
            if rag:
                extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

        router_result = await self._router.infer(
            domain=domain,
            user_text=text,
            screenshot_b64=screenshot_b64,
            context=extra_ctx,
        )

        self._push_context(f"User: {text}\nAssistant ({router_result.model}): {router_result.text[:200]}")

        result = AgentResult(
            goal=text,
            domain=domain,
            model_used=router_result.model,
            response_text=router_result.text,
            success=router_result.ok,
            error=router_result.error,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        await self._persist_run(result, command_id=None)

        # Speak EXPLAIN responses via Polly Bidirectional Streaming TTS.
        # The Node.js sidecar handles StartSpeechSynthesisStream internally, so
        # even a complete-string POST benefits from the Generative engine and
        # 24kHz audio quality. True token-by-token streaming to TTS would require
        # ModelRouter.infer_stream() — a future enhancement.
        if router_result.text and router_result.ok:
            try:
                from tts.polly_stream import get_client as _get_tts
                asyncio.create_task(_get_tts().speak(router_result.text))
            except Exception as _tts_exc:
                log.debug("DevAgent TTS failed: %s", _tts_exc)

        return result

    # ---------------------------------------------------------------------- #
    # Plan → Execute → Reflect loop
    # ---------------------------------------------------------------------- #

    async def plan_and_run(self, goal: str) -> AgentResult:
        """Decompose a complex goal into steps and execute them sequentially."""
        t0 = time.monotonic()
        log.info("DevAgent: planning goal %r", goal[:80])

        # Unified agent-run trace (gap C): one trace_id spans the whole plan.
        # Setting it as the current ContextVar means every awaited descendant —
        # ModelRouter.infer's inference spans, scheduler.fan_out children — attach
        # to THIS trace automatically, reconstructing the run as one tree. Zero
        # cost when DA_TRACE is off (new_trace returns "" and spans no-op).
        from monitoring.trace import get_tracer
        _tracer = get_tracer()
        trace_id = _tracer.new_trace(kind="plan", goal=goal[:80])
        _trace_tok = _tracer.set_current(trace_id)
        _tracer.record_span("plan", trace_id=trace_id, goal=goal[:80])

        # Step 1: Generate plan — inject RAG context + git/IDE context
        extra_ctx = self._format_context()
        rag = await self._rag_context(goal, n=4)
        if rag:
            extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

        # Git context injection (item #8): gives LLM branch/diff awareness
        git_ctx = await self._git_context()
        if git_ctx:
            extra_ctx = f"{git_ctx}\n\n{extra_ctx}" if extra_ctx else git_ctx

        plan_result = await self._router.infer(
            domain="plan",
            user_text=goal,
            context=extra_ctx,
        )
        if not plan_result.ok:
            _tracer.record_span("plan_done", trace_id=trace_id, status="plan_error")
            _tracer.reset_current(_trace_tok)
            return AgentResult(
                goal=goal, domain="plan",
                model_used=plan_result.model,
                error=plan_result.error,
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        steps = _parse_plan(plan_result.text)
        if not steps:
            # Planner returned free-form — treat as single EXPLAIN step
            steps = [AgentStep(action="EXPLAIN", body=plan_result.text)]

        log.info("DevAgent: plan has %d steps", len(steps))

        # Upfront plan approval gate: speak summary → voice yes/no → authorize all steps
        self._plan_authorized = await self._approve_plan_upfront(goal, steps)
        self._cancel_event.clear()
        self._current_goal = goal
        self._total_steps = min(len(steps), self.MAX_STEPS)
        self._current_step = 0

        # Durable ledger: write a 'running' run row now so a crash mid-plan is
        # recoverable (reconciled to 'interrupted' on next startup).
        run_id = await self._start_run(goal, plan_result.model)

        # Step 2: Closed-loop execution (observe → act → replan-on-failure).
        # A failed step never blindly continues (that compounds errors): the
        # controller asks the planner for a bounded recovery plan, and halts if
        # none is available or the replan budget is spent.
        executed: list[AgentStep] = []
        remaining: list[AgentStep] = list(steps[: self.MAX_STEPS])
        replans = 0
        cancelled = False
        halted_reason: Optional[str] = None

        # Parallel context-gathering (gap #1): fan out a leading run of independent
        # read-only steps before the sequential action loop. No-op without a
        # scheduler or with < 2 such steps; failure falls back to sequential.
        await self._gather_readonly_prefix(remaining, executed, run_id)

        while remaining:
            if len(executed) >= self.MAX_STEPS:
                halted_reason = f"reached MAX_STEPS ({self.MAX_STEPS})"
                log.warning("DevAgent: %s", halted_reason)
                break
            if self._cancel_event.is_set():
                log.info("DevAgent: plan cancelled at step %d", len(executed) + 1)
                cancelled = True
                break

            step = remaining.pop(0)
            self._current_step = len(executed) + 1
            self._total_steps = len(executed) + 1 + len(remaining)
            log.info("DevAgent: step %d  action=%s  args=%r",
                     self._current_step, step.action, step.args[:60])

            ok = await self._run_step_with_retry(step)
            _tracer.record_span("step", trace_id=trace_id, action=step.action, ok=ok)
            executed.append(step)
            await self._persist_step(run_id, len(executed), step)
            if ok:
                continue

            # Step failed — try a bounded recovery replan; otherwise halt.
            if replans < self.MAX_REPLANS and not self._cancel_event.is_set():
                replans += 1
                new_steps = await self._replan(goal, executed, remaining)
                if new_steps:
                    budget = max(0, self.MAX_STEPS - len(executed))
                    remaining = new_steps[:budget]
                    self._total_steps = len(executed) + len(remaining)
                    log.info(
                        "DevAgent: replanned after failed %s — %d new step(s) (replan %d/%d)",
                        step.action, len(remaining), replans, self.MAX_REPLANS,
                    )
                    continue
            halted_reason = f"halted after failed {step.action} (no recovery plan)"
            log.warning("DevAgent: %s", halted_reason)
            break

        # Step 3: Reflect — summarise outcomes for the user.
        reflect_text = await self._reflect(goal, executed, plan_result.model)

        self._push_context(
            f"Completed plan: {goal}\n"
            + "\n".join(f"  {s.action} → {'ok' if s.success else 'failed'}" for s in executed)
        )

        succeeded = (not cancelled) and (halted_reason is None)
        result = AgentResult(
            goal=goal,
            domain="plan",
            model_used=plan_result.model,
            steps=executed,
            response_text=reflect_text or plan_result.text,
            success=succeeded,
            total_latency_ms=(time.monotonic() - t0) * 1000,
        )
        self._results_log.append(result)
        status = "cancelled" if cancelled else ("completed" if succeeded else "failed")
        await self._finalize_run(run_id, result, status)

        # Speak completion summary and clean up goal-session state
        await self._speak_plan_completion(result, cancelled)
        self._reset_plan_state()

        _tracer.record_span("plan_done", trace_id=trace_id,
                            steps=len(executed), success=succeeded, status=status)
        _tracer.reset_current(_trace_tok)
        return result

    async def _run_step_with_retry(self, step: AgentStep) -> bool:
        """Execute one step, retrying once for retryable (read-only) verbs.

        Records result/success/latency on `step` and returns its success bool.
        Destructive verbs are attempted exactly once (no retry).
        """
        attempts = 2 if step.action.upper() in self._RETRYABLE_VERBS else 1
        for attempt in range(attempts):
            step_t0 = time.monotonic()
            try:
                step.result = await self._execute_step(step)
                step.success = True
                step.latency_ms = (time.monotonic() - step_t0) * 1000
                return True
            except Exception as exc:
                step.result = f"ERROR: {exc}"
                step.success = False
                step.latency_ms = (time.monotonic() - step_t0) * 1000
                log.error(
                    "DevAgent: step %s failed (attempt %d/%d): %s",
                    step.action, attempt + 1, attempts, exc,
                )
        return False

    # ── Parallel context-gathering (gap #1) ─────────────────────────────────

    def _leading_parallel_prefix(self, remaining: list[AgentStep]) -> list[AgentStep]:
        """The leading contiguous run of pure read-only steps at the front of `remaining`."""
        prefix: list[AgentStep] = []
        for s in remaining:
            if s.action.upper() in self._PARALLEL_VERBS:
                prefix.append(s)
            else:
                break
        return prefix

    async def _gather_readonly_prefix(
        self,
        remaining: list[AgentStep],
        executed: list[AgentStep],
        run_id: int,
    ) -> None:
        """Fan out a leading run of independent read-only steps concurrently.

        Closes gap #1 for the common 'gather context, then act' plan shape. No-op
        unless a scheduler is wired and >= 2 such steps lead the plan.

        Failure semantics are preserved EXACTLY: on any failed/timed-out child the
        parallel results are discarded and the steps are left in `remaining` for
        the normal sequential loop (which applies retry + replan-on-failure). Read
        verbs are idempotent, so the rare re-run is safe and side-effect-free.
        """
        if self._scheduler is None or self._cancel_event.is_set():
            return
        prefix = self._leading_parallel_prefix(remaining)
        if len(prefix) < 2:
            return

        results = await self._scheduler.fan_out(
            [self._run_step_with_retry(s) for s in prefix],
            label=f"devagent_readonly[{len(prefix)}]",
        )
        if not all(r is True for r in results):
            log.info(
                "DevAgent: parallel read-only prefix had a failure — falling back "
                "to sequential execution (replan-on-failure preserved)"
            )
            return

        # All succeeded — adopt the batch: advance `executed` and persist in order.
        del remaining[: len(prefix)]
        for s in prefix:
            executed.append(s)
            await self._persist_step(run_id, len(executed), s)
        self._current_step = len(executed)
        self._total_steps = len(executed) + len(remaining)
        log.info("DevAgent: ran %d read-only context step(s) in parallel", len(prefix))

    async def _replan(
        self, goal: str, executed: list[AgentStep], remaining: list[AgentStep]
    ) -> list[AgentStep]:
        """Ask the planner for a revised plan for the REMAINING work after a failure.

        Feeds the executed steps + their outcomes (the observation signal) back to
        the plan-domain model so it can recover. Returns parsed steps, or [] if the
        planner errors or declines.
        """
        lines = [f"Goal: {goal}", "", "Steps already executed (with outcomes):"]
        for i, s in enumerate(executed, 1):
            status = "ok" if s.success else "FAILED"
            snippet = (s.result or "")[:300]
            lines.append(f"  {i}. [{status}] {s.action} {s.args[:60]} → {snippet}")
        if remaining:
            lines.append("")
            lines.append("Original remaining steps (not yet run):")
            for s in remaining:
                lines.append(f"  [{s.action} {s.args[:60]}]")
        lines += [
            "",
            "The last step FAILED. Produce a REVISED numbered plan for the remaining "
            "work that recovers from the failure, using the same [ACTION args] step "
            "format. Do not repeat already-completed work. If the goal cannot proceed, "
            "reply with a single [EXPLAIN <reason>] step.",
        ]
        prompt = "\n".join(lines)
        try:
            r = await self._router.infer(domain="plan", user_text=prompt, context=None)
            if r.ok and r.text:
                return _parse_plan(r.text)
        except Exception as exc:
            log.debug("DevAgent._replan failed: %s", exc)
        return []

    # ---------------------------------------------------------------------- #
    # Durable plan ledger (resumable across crashes)
    # ---------------------------------------------------------------------- #

    def _db(self):
        """The AgentDB handle (via MemoryManager when wired, else direct)."""
        if self._memory is not None:
            return getattr(self._memory, "_db", None)
        return self._agent_db

    async def _start_run(self, goal: str, model_used: Optional[str]) -> int:
        db = self._db()
        if not db or not getattr(db, "available", False):
            return -1
        try:
            return await db.start_agent_run(goal=goal, domain="plan", model_used=model_used)
        except Exception as exc:
            log.debug("DevAgent._start_run failed: %s", exc)
            return -1

    async def _persist_step(self, run_id: int, step_num: int, step: AgentStep) -> None:
        db = self._db()
        if run_id < 0 or not db or not getattr(db, "available", False):
            return
        try:
            await db.insert_agent_step(
                run_id=run_id, step_num=step_num, action=step.action,
                args=step.args or None, body=step.body or None,
                result=step.result, success=step.success, latency_ms=step.latency_ms,
            )
        except Exception as exc:
            log.debug("DevAgent._persist_step failed: %s", exc)

    async def _finalize_run(self, run_id: int, result: AgentResult, status: str) -> None:
        db = self._db()
        if run_id < 0 or not db or not getattr(db, "available", False):
            return
        try:
            await db.update_agent_run(
                run_id=run_id, status=status, step_count=len(result.steps),
                success=result.success, total_latency_ms=result.total_latency_ms,
                error=result.error,
            )
        except Exception as exc:
            log.debug("DevAgent._finalize_run failed: %s", exc)

    async def resume_pending_plan(self) -> Optional[dict]:
        """Offer to resume the most recent interrupted plan, gated on voice confirm.

        Accessibility safety: never auto-resumes — an interrupted plan may contain
        destructive steps, so it requires an explicit spoken "yes" (via
        _confirm_destructive_op). Re-runs plan_and_run for the goal (a fresh plan
        that the closed-loop controller adapts), and returns the resumed run dict,
        or None if there's nothing to resume / the user declines.
        """
        db = self._db()
        if not db or not getattr(db, "available", False):
            return None
        runs = await db.get_interrupted_runs(limit=1)
        if not runs:
            return None
        run = runs[0]
        goal = run.get("goal", "")
        if not await self._confirm_destructive_op(f"Resume the interrupted task: {goal[:60]}?"):
            log.info("DevAgent.resume_pending_plan: user declined resume of run %s", run.get("id"))
            return None
        log.info("DevAgent.resume_pending_plan: resuming run %s — %r", run.get("id"), goal[:60])
        await self.plan_and_run(goal)
        return run

    # ---------------------------------------------------------------------- #
    # Plan-level authorization helpers
    # ---------------------------------------------------------------------- #

    async def _approve_plan_upfront(self, goal: str, steps: list[AgentStep]) -> bool:
        """Speak plan summary, capture voice yes/no, write GoalSession on approval.

        Returns True if the user approved (or TTS/mic unavailable — auto-approve
        so a hardware failure never silently drops work).
        """
        from core.goal_session import GoalSessionStore

        verbs = [s.action for s in steps[: self.MAX_STEPS]]
        verb_summary = ", ".join(verbs[:6])
        if len(verbs) > 6:
            verb_summary += f" … (+{len(verbs) - 6} more)"
        n = min(len(steps), self.MAX_STEPS)
        message = f"I'll run {n} step{'s' if n != 1 else ''}: {verb_summary}. Approve all?"

        log.info("DevAgent: requesting plan approval — %s", message)

        plan_is_destructive = any(
            s.action.upper() in self._DESTRUCTIVE_VERBS for s in steps[: self.MAX_STEPS]
        )

        def _grant() -> bool:
            GoalSessionStore.create(goal=goal, domain="plan")
            return True

        def _fallback(reason: str) -> bool:
            """No clear consent obtained (hardware failure / silence / ambiguity).

            Read-only plans auto-approve for convenience; destructive plans
            fail-safe to DENY — never run side effects without an explicit yes.
            """
            if plan_is_destructive:
                log.info(
                    "DevAgent._approve_plan_upfront: %s + destructive plan → DENY", reason
                )
                return False
            log.info(
                "DevAgent._approve_plan_upfront: %s + read-only plan → auto-approve", reason
            )
            return _grant()

        # Speak via TTS
        try:
            from tts.polly_stream import get_client as _get_tts
            await asyncio.to_thread(_get_tts().speak_sync, message)
        except Exception as exc:
            return _fallback(f"TTS unavailable ({exc})")

        # Wait for iPad approval signal (7 s window, same as approval_hook.py)
        _APPROVAL_DIR = Path.home() / ".claude" / "approval"
        _PENDING_FILE  = _APPROVAL_DIR / "pending"
        _RESPONSE_FILE = _APPROVAL_DIR / "response"

        _APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
        _RESPONSE_FILE.unlink(missing_ok=True)
        _PENDING_FILE.write_text(str(time.monotonic()), encoding="utf-8")

        transcript: Optional[str] = None
        deadline = time.monotonic() + 7.0
        try:
            while time.monotonic() < deadline:
                if _RESPONSE_FILE.exists():
                    transcript = _RESPONSE_FILE.read_text(encoding="utf-8-sig").strip()
                    break
                await asyncio.sleep(0.1)
        finally:
            _PENDING_FILE.unlink(missing_ok=True)
            _RESPONSE_FILE.unlink(missing_ok=True)

        if transcript is None:
            # Silence or bridge not running → fallback: 4 s PC mic recording
            try:
                import numpy as np
                import sounddevice as sd
                audio = await asyncio.to_thread(
                    lambda: sd.rec(int(4.0 * 16_000), samplerate=16_000,
                                   channels=1, dtype="float32").flatten()
                )
                await asyncio.to_thread(sd.wait)
                rms = float(np.sqrt(np.mean(audio ** 2)))
                if rms < 0.005:
                    return _fallback("silence")
                if self._confirm_whisper is None:
                    from faster_whisper import WhisperModel
                    self._confirm_whisper = WhisperModel("tiny", device="cpu", compute_type="int8")
                segs, _ = self._confirm_whisper.transcribe(audio, language="en", beam_size=1, vad_filter=False)
                transcript = " ".join(s.text for s in segs).lower().strip()
            except Exception as exc:
                return _fallback(f"mic fallback failed ({exc})")

        # Shared confirmation vocabulary (core/approval_keywords). An explicit
        # deny always blocks. An explicit yes grants. Anything else (ambiguous /
        # unrecognised) defers to _fallback: auto-approve for read-only plans,
        # fail-safe DENY for destructive ones.
        verdict = classify_confirmation(transcript)
        if verdict == "deny":
            log.info("DevAgent._approve_plan_upfront: REJECTED — %r", transcript)
            return False
        if verdict == "approve":
            log.info("DevAgent._approve_plan_upfront: approved — %r", transcript)
            return _grant()
        return _fallback(f"ambiguous reply {transcript!r}")

    async def _speak_plan_completion(self, result: AgentResult, cancelled: bool) -> None:
        """Speak a short TTS summary after a plan finishes."""
        if cancelled:
            msg = (f"Task cancelled at step {self._current_step} of {self._total_steps}.")
        elif result.success:
            summary = (result.response_text or "")[:80].replace("\n", " ")
            msg = f"Done. {summary}" if summary else "Plan complete."
        else:
            failed = [s for s in result.steps if not s.success]
            first_err = (failed[0].result or "")[:60] if failed else ""
            msg = f"Task failed at step {self._current_step}: {first_err}" if first_err else "Plan failed."
        try:
            from tts.polly_stream import get_client as _get_tts
            asyncio.create_task(_get_tts().speak(msg))
        except Exception as exc:
            log.debug("DevAgent._speak_plan_completion: TTS failed: %s", exc)

    def _reset_plan_state(self) -> None:
        """Clean up goal-session and status fields after a plan run."""
        from core.goal_session import GoalSessionStore
        GoalSessionStore.cancel()
        self._plan_authorized = False
        self._cancel_event.clear()
        self._current_goal = None
        self._current_step = 0
        self._total_steps = 0

    async def _reflect(
        self, goal: str, steps: list[AgentStep], model: str
    ) -> Optional[str]:
        """Reflect on executed steps: summarise outcomes, flag failures.

        Sends a lightweight prompt that includes each step's action + result
        so the model can reason about what was actually accomplished.
        Returns the reflection text, or None on failure.
        """
        if not steps:
            return None

        # Build step summary — include full result for failed steps so the
        # model can diagnose; truncate successes to avoid prompt bloat.
        lines = [f"Goal: {goal}", "", "Steps executed:"]
        for i, s in enumerate(steps, 1):
            status = "✓" if s.success else "✗"
            result_snippet = (s.result or "")
            if s.success:
                result_snippet = result_snippet[:200]
            else:
                result_snippet = result_snippet[:600]   # full error for failures
            lines.append(
                f"  {i}. {status} {s.action} {s.args[:60]}\n"
                f"     → {result_snippet}"
            )

        lines += [
            "",
            "Briefly summarise: what was accomplished, what (if anything) failed,"
            " and what the user should know or do next.",
        ]
        reflect_prompt = "\n".join(lines)

        try:
            r = await self._router.infer(
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

    # ---------------------------------------------------------------------- #
    # Step execution
    # ---------------------------------------------------------------------- #

    async def _execute_step(self, step: AgentStep) -> str:
        action = step.action.upper()

        if action == "WRITE_FILE":
            return await asyncio.to_thread(self._write_file, step.args, step.body)

        if action == "RUN_TERMINAL":
            cmd = step.args or step.body
            return await asyncio.to_thread(self._run_terminal, cmd)

        if action == "EXPLAIN":
            # Return text to the caller; no desktop action
            return step.body or step.args

        if action == "SEARCH_WEB":
            query = step.args or step.body
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            await asyncio.to_thread(webbrowser.open, url)
            return f"Opened browser: {url}"

        if action == "READ_SCREEN":
            b64 = await self._capture_screenshot()
            if b64 and step.args:
                # Ask vision model the question in args
                r = await self._router.analyse_screen(b64, step.args)
                return r.text
            return "Screenshot captured"

        if action == "READ_FILE":
            path_str = step.args or step.body
            return await asyncio.to_thread(self._read_file, path_str.strip())

        if action == "GREP":
            # args format: "PATTERN [PATH]"  — path optional, defaults to project root
            parts = step.args.split(None, 1)
            pattern = parts[0] if parts else step.body
            search_path = parts[1].strip() if len(parts) > 1 else "."
            return await asyncio.to_thread(self._grep, pattern, search_path)

        # ── Git-native verbs (roadmap item #3) ──────────────────────────────

        if action == "GIT_STATUS":
            return await asyncio.to_thread(self._git_status)

        if action == "GIT_DIFF":
            # args: optional "--staged" or a file path
            flags = (step.args or "").strip()
            return await asyncio.to_thread(self._git_diff, flags)

        if action == "GIT_COMMIT":
            # args: commit message
            msg = (step.args or step.body or "").strip()
            if not msg:
                raise ValueError("GIT_COMMIT requires a commit message")
            if not await self._confirm_destructive_op(
                f"Approve git commit: {msg[:60]}?"
            ):
                return "GIT_COMMIT cancelled by user"
            return await asyncio.to_thread(self._git_commit, msg)

        if action == "GIT_CHECKOUT":
            # args: [-b] <branch>
            branch_args = (step.args or "").strip()
            if not await self._confirm_destructive_op(
                f"Approve git checkout {branch_args[:40]}?"
            ):
                return "GIT_CHECKOUT cancelled by user"
            return await asyncio.to_thread(self._git_checkout, branch_args)

        # ── GitHub integration (roadmap item #3) ────────────────────────────

        if action == "GITHUB_PR":
            # args: title  body: PR description
            title = (step.args or "").strip()
            body = (step.body or "").strip()
            if not title:
                raise ValueError("GITHUB_PR requires a title in args")
            if not await self._confirm_destructive_op(
                f"Approve opening pull request: {title[:60]}?"
            ):
                return "GITHUB_PR cancelled by user"
            return await asyncio.to_thread(self._github_pr, title, body)

        # ── Web retrieval (roadmap item #3) ─────────────────────────────────

        if action == "FETCH_URL":
            url = (step.args or step.body or "").strip()
            if not url:
                raise ValueError("FETCH_URL requires a URL")
            return await self._fetch_url(url)

        # Fall through: accessibility verbs → CommandExecutor
        if self._coordinator:
            from core.command_executor import Command
            cmd = Command(
                text=step.args,
                action=action,
                source="dev_agent",
                params=self._parse_accessibility_params(action, step.args),
            )
            result_dict = await self._coordinator._executor.execute(cmd)
            return json.dumps(result_dict)

        return f"No executor for action: {action}"

    # ---------------------------------------------------------------------- #
    # Dev action implementations
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _write_file(path_str: str, content: str) -> str:
        path = Path(path_str.strip().strip("'\""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.info("DevAgent: wrote %d bytes to %s", len(content), path)
        return f"Written {len(content)} bytes to {path}"

    @staticmethod
    def _read_file(path_str: str, max_chars: int = 8000) -> str:
        """Read a file and return its contents (truncated to max_chars)."""
        path = Path(path_str.strip("'\""))
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n… [truncated at {max_chars} chars]"
        log.info("DevAgent: read %d chars from %s", len(text), path)
        return text

    @staticmethod
    def _grep(pattern: str, search_path: str, max_lines: int = 100) -> str:
        """Search for a regex pattern in files under search_path.

        Returns matching lines as a string (file:line: content format).
        Uses Python re for portability — no dependency on system grep.
        """
        import os as _os

        root = Path(search_path)
        if not root.exists():
            return f"Path does not exist: {search_path}"

        compiled = re.compile(pattern)
        results: list[str] = []
        extensions = {".py", ".swift", ".md", ".txt", ".json", ".yaml", ".yml"}

        def _search_file(fp: Path) -> None:
            if len(results) >= max_lines:
                return
            try:
                for lineno, line in enumerate(
                    fp.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if compiled.search(line):
                        results.append(f"{fp}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_lines:
                            break
            except OSError:
                pass

        if root.is_file():
            _search_file(root)
        else:
            for dirpath, dirnames, filenames in _os.walk(root):
                # Prune excluded directories in-place
                dirnames[:] = [
                    d for d in dirnames
                    if d not in {"__pycache__", ".git", "node_modules",
                                 "venv", ".venv", "chroma_db", "DerivedData"}
                ]
                for fname in filenames:
                    if Path(fname).suffix in extensions:
                        _search_file(Path(dirpath) / fname)
                    if len(results) >= max_lines:
                        break

        if not results:
            return f"No matches for pattern {pattern!r} in {search_path}"
        summary = f"Found {len(results)} match(es)"
        if len(results) >= max_lines:
            summary += f" (truncated at {max_lines})"
        return summary + "\n" + "\n".join(results)

    @staticmethod
    def _run_terminal(cmd: str) -> str:
        cmd = cmd.strip()
        log.info("DevAgent: running terminal command: %s", cmd)
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
        log.info("DevAgent: terminal %s → %s", status, output[:120])
        if result.returncode != 0:
            raise RuntimeError(f"Command failed ({status}): {output[:200]}")
        return output or status

    # ── Git safety confirmation ──────────────────────────────────────────────

    # Verbs that mutate state visible to others or that are hard to reverse.
    _GIT_DESTRUCTIVE_VERBS: frozenset[str] = frozenset({
        "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR"
    })

    async def _confirm_destructive_op(self, description: str) -> bool:
        """Speak the action description and wait for voice confirmation.

        This op is destructive by definition, so it fails SAFE to DENY: only an
        explicit spoken "yes" (or a prior whole-plan authorization) proceeds.
        Silence, an ambiguous reply, or unavailable TTS/microphone all return
        False — the op is skipped rather than run without clear consent. Mirrors
        the hardened voice approval gate (approval_hook.py, timeout→reject).
        """
        import numpy as np

        # If the user already approved the entire plan upfront, skip per-op confirmation
        if self._plan_authorized:
            log.info("DevAgent._confirm: skipping (plan authorized) — %s", description)
            return True

        log.info("DevAgent: confirmation required — %s", description)

        # --- 1. Speak via TTS ------------------------------------------------
        try:
            from tts.polly_stream import get_client as _get_tts
            _tts = _get_tts()
            await asyncio.to_thread(_tts.speak_sync, description)
        except Exception as exc:
            log.info("DevAgent._confirm: TTS unavailable (%s) — DENY (fail-safe)", exc)
            return False

        # --- 2. Record 4 s of mic audio --------------------------------------
        try:
            import sounddevice as sd
            audio = await asyncio.to_thread(
                lambda: sd.rec(
                    int(4.0 * 16_000), samplerate=16_000,
                    channels=1, dtype="float32",
                ).flatten()
            )
            await asyncio.to_thread(sd.wait)
        except Exception as exc:
            log.info("DevAgent._confirm: mic unavailable (%s) — DENY (fail-safe)", exc)
            return False

        # --- 3. Check for voice activity; silence → DENY (fail-safe) ---------
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < 0.005:
            log.info("DevAgent._confirm: silence → DENY (fail-safe)")
            return False

        # --- 4. Transcribe with tiny Whisper on CPU (no GPU contention) ------
        # Model is cached on self so the ~600ms load cost is paid once.
        try:
            if self._confirm_whisper is None:
                from faster_whisper import WhisperModel
                self._confirm_whisper = WhisperModel("tiny", device="cpu", compute_type="int8")
            model = self._confirm_whisper
            segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False)
            text = " ".join(s.text for s in segs).lower().strip()
            log.info("DevAgent._confirm: heard %r", text)
        except Exception as exc:
            log.info("DevAgent._confirm: transcription failed (%s) — DENY (fail-safe)", exc)
            return False

        # --- 5. Keyword detection (shared vocabulary, core/approval_keywords) -
        verdict = classify_confirmation(text)
        if verdict == "deny":
            log.info("DevAgent._confirm: REJECTED — %r", text)
            return False
        if verdict == "approve":
            log.info("DevAgent._confirm: approved — %r", text)
            return True

        # Ambiguous / unrecognised reply → DENY. This op is destructive, so the
        # only outcome that proceeds is an explicit "yes" (handled above).
        log.info("DevAgent._confirm: ambiguous response %r → DENY (fail-safe)", text)
        return False

    # ── Git implementations ──────────────────────────────────────────────────

    @staticmethod
    def _git_status() -> str:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            raise RuntimeError(f"git status failed: {result.stderr.strip()[:200]}")
        return out or "(nothing to commit, working tree clean)"

    @staticmethod
    def _git_diff(flags: str = "", max_chars: int = 8000) -> str:
        cmd = ["git", "diff"]
        if flags:
            cmd.extend(flags.split())
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip()[:200]}")
        out = result.stdout.strip()
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n… [truncated at {max_chars} chars]"
        return out or "(no diff)"

    @staticmethod
    def _git_commit(message: str) -> str:
        # Stage all tracked changes then commit
        subprocess.run(["git", "add", "-u"], check=True, timeout=10)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr.strip()[:200]}")
        out = result.stdout.strip()
        log.info("DevAgent: git commit — %s", out[:100])
        return out

    @staticmethod
    def _git_checkout(branch_args: str) -> str:
        cmd = ["git", "checkout"] + branch_args.split()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed: {result.stderr.strip()[:200]}")
        return result.stdout.strip() or result.stderr.strip() or "ok"

    @staticmethod
    def _github_pr(title: str, body: str) -> str:
        """Create a GitHub PR using the gh CLI and return the PR URL."""
        cmd = ["gh", "pr", "create", "--title", title]
        if body:
            cmd.extend(["--body", body])
        else:
            cmd.extend(["--body", "Created by Personal Desktop Agent via voice command."])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {result.stderr.strip()[:200]}")
        url = result.stdout.strip()
        log.info("DevAgent: PR created — %s", url)
        return url

    async def _fetch_url(self, url: str, max_chars: int = 6000) -> str:
        """Fetch a URL and return extracted text (replaces browser-open SEARCH_WEB)."""
        try:
            import aiohttp
        except ImportError:
            # Fall back to webbrowser open (old behaviour)
            await asyncio.to_thread(webbrowser.open, url)
            return f"Opened browser: {url}"

        headers = {"User-Agent": "Mozilla/5.0 (compatible; DesktopAgent/1.0)"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content_type = resp.content_type or ""
                    if "html" in content_type:
                        html = await resp.text(errors="replace")
                        text = _strip_html(html)
                    else:
                        text = await resp.text(errors="replace")
                    if len(text) > max_chars:
                        text = text[:max_chars] + f"\n… [truncated at {max_chars}]"
                    log.info("DevAgent: fetched %s (%d chars)", url, len(text))
                    return text
        except Exception as exc:
            raise RuntimeError(f"FETCH_URL {url} failed: {exc}") from exc

    # ── Context helpers ──────────────────────────────────────────────────────

    async def _git_context(self) -> Optional[str]:
        """Fetch git state for plan prompt injection.

        Tries KiroClient first (richer VS Code git data), falls back to
        subprocess git commands directly.
        """
        # Try Kiro first
        if self._kiro is not None:
            git = await self._kiro.get_git_context()
            if git and "error" not in git:
                return self._kiro.format_git_context_for_prompt(git)

        # Subprocess fallback
        try:
            result = await asyncio.to_thread(
                lambda: subprocess.run(
                    ["git", "status", "--short", "--branch"],
                    capture_output=True, text=True, timeout=5,
                )
            )
            if result.returncode == 0 and result.stdout.strip():
                out = result.stdout.strip()
                return f"```git-context\n{out}\n```"
        except Exception as exc:
            log.debug("DevAgent._git_context() subprocess fallback failed: %s", exc)

        return None

    @staticmethod
    async def _capture_screenshot() -> Optional[str]:
        try:
            from mcp_server.tools import screen as _screen
            result = await asyncio.to_thread(_screen.screenshot)
            return result.get("image_base64")
        except Exception as exc:
            log.warning("DevAgent: screenshot failed: %s", exc)
            return None

    @staticmethod
    def _parse_accessibility_params(action: str, args: str) -> dict:
        params: dict = {}
        if action == "SCROLL":
            words = args.lower().split()
            for w in words:
                if w in ("up", "down", "left", "right"):
                    params["direction"] = w
                    break
            for w in words:
                try:
                    params["amount"] = int(w)
                    break
                except ValueError:
                    pass
        elif action in ("TYPE", "DICTATE"):
            params["text"] = args
        elif action == "OPEN":
            params["target"] = args
        elif action == "HOTKEY":
            keys = [k.strip() for k in re.split(r"[+\s]+", args) if k.strip()]
            params["keys"] = keys
        return params

    # ---------------------------------------------------------------------- #
    # DB persistence
    # ---------------------------------------------------------------------- #

    async def _persist_run(
        self, result: AgentResult, command_id: Optional[int]
    ) -> None:
        # Phase B: route through MemoryManager when available; fall back to
        # direct AgentDB calls so existing behaviour is preserved when
        # MemoryManager is not wired (e.g. in unit tests).
        if self._memory is not None:
            run_id = -1
            try:
                run_id = await self._memory._db.insert_agent_run(
                    command_id=command_id,
                    goal=result.goal,
                    domain=result.domain,
                    model_used=result.model_used,
                    step_count=len(result.steps),
                    success=result.success,
                    total_latency_ms=result.total_latency_ms,
                    error=result.error,
                )
                for i, step in enumerate(result.steps):
                    await self._memory.write_state(
                        "agent_step",
                        {
                            "run_id": run_id,
                            "step_num": i + 1,
                            "action": step.action,
                            "args": step.args or None,
                            "body": step.body or None,
                            "result": step.result,
                            "success": step.success,
                            "latency_ms": step.latency_ms,
                        },
                        namespace="dev_agent",
                    )
            except Exception as exc:
                log.warning("DevAgent._persist_run via MemoryManager failed: %s", exc)
            return

        if not self._agent_db or not self._agent_db.available:
            return
        run_id = await self._agent_db.insert_agent_run(
            command_id=command_id,
            goal=result.goal,
            domain=result.domain,
            model_used=result.model_used,
            step_count=len(result.steps),
            success=result.success,
            total_latency_ms=result.total_latency_ms,
            error=result.error,
        )
        for i, step in enumerate(result.steps):
            await self._agent_db.insert_agent_step(
                run_id=run_id,
                step_num=i + 1,
                action=step.action,
                args=step.args or None,
                body=step.body or None,
                result=step.result,
                success=step.success,
                latency_ms=step.latency_ms,
            )

    # ---------------------------------------------------------------------- #
    # Context management
    # ---------------------------------------------------------------------- #

    def _push_context(self, entry: str) -> None:
        self._context.append(entry)
        if len(self._context) > 10:
            self._context = self._context[-10:]

    def _format_context(self) -> Optional[str]:
        if not self._context:
            return None
        return "\n".join(self._context[-5:])

    async def _rag_context(self, query: str, n: int = 3) -> Optional[str]:
        """Fetch top-n relevant source chunks from CodebaseIndexer for `query`.

        Returns a formatted string block suitable for injection as extra context
        in the system/user prompt, or None if the indexer is unavailable or returns
        no useful hits.
        """
        # Prefer the laptop indexer service when configured and healthy.
        hits = None
        use_remote = self._remote_indexer is not None and (
            self._cluster_health is None or self._cluster_health.is_healthy("indexer")
        )
        if use_remote:
            try:
                hits = await self._remote_indexer.query_combined(query, n=n)
            except Exception as exc:
                log.debug("DevAgent._rag_context() remote indexer failed: %s — local fallback", exc)
                hits = None

        # M3: an empty remote result (flaking service returning []) is treated as
        # a miss, not success — fall back to the local indexer rather than
        # silently dropping RAG context.
        if not hits:
            if self._indexer is None or not self._indexer.available:
                return None
            try:
                hits = await self._indexer.query_combined(query, n=n)
            except Exception as exc:
                log.debug("DevAgent._rag_context() failed: %s", exc)
                return None

        try:
            if not hits:
                return None
            lines = ["[Relevant codebase context]"]
            for h in hits:
                if h.get("chunk_type") == "page":
                    lines.append(
                        f"# {h['file']} p.{h.get('page')} (score={h.get('score', 0):.2f})"
                    )
                else:
                    lines.append(
                        f"# {h['file']}::{h.get('name')} [{h.get('chunk_type')}]"
                        f" line {h.get('start_line', '?')} (score={h.get('score', 0):.2f})"
                    )
                snippet = (h.get("text") or "")[:600]
                lines.append(snippet)
                lines.append("")
            return "\n".join(lines)
        except Exception as exc:
            log.debug("DevAgent._rag_context() failed: %s", exc)
            return None

    # ---------------------------------------------------------------------- #
    # Status / introspection
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "classifier": "DomainClassifier (keyword-scoring)",
            "router": self._router.get_status(),
            "context_entries": len(self._context),
            "tasks_completed": len(self._results_log),
            "rag_indexer": (
                "wired" if (self._indexer and self._indexer.available)
                else "not wired" if self._indexer is None
                else "unavailable"
            ),
            "kiro_bridge": (
                self._kiro.get_status() if self._kiro is not None else "not wired"
            ),
            "plan": self.get_plan_status(),
        }

    def get_last_result(self) -> Optional[AgentResult]:
        return self._results_log[-1] if self._results_log else None
