"""DevAgent — agentic task loop for software development and research tasks.

Implements a plan → execute → observe → reflect loop that lets the agent
autonomously complete multi-step development tasks using specialist models
and an expanded action vocabulary.

Expanded action verbs (beyond the 9 accessibility verbs):
  WRITE_FILE <path>   — create or fully overwrite a file
  EDIT_FILE <path>    — surgically edit an existing file via SEARCH/REPLACE blocks
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
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
from inference.model_router import ModelRouter, RouterResult

if TYPE_CHECKING:
    from inference.codebase_indexer import CodebaseIndexer
    from adaptive.continuous_trainer import ContinuousTrainer
    from storage.db import AgentDB
    from core.hybrid_coordinator import HybridCoordinator
    from inference.bridge_client import BridgeClient

log = logging.getLogger(__name__)


# RAG taint fences + trust-classifier / content-filter singletons moved to
# inference/dev_common.py during the god-object split (shared with
# step_executor and context_builder without a circular import).

# ---------------------------------------------------------------------------
# Step model
# ---------------------------------------------------------------------------

# Action verbs the planner model is allowed to emit


# Planner teaching for the DELEGATE verb — injected into the plan context ONLY when
# DA_DELEGATE is on (specs/dev-agent-delegate-verb R4.4), so the planner vocabulary
# is byte-identical to today when the feature is off.

# Personal-document query detection lives in storage.personal_kb so the
# coordinator can share it (forcing such queries local) without importing this
# heavier module.
from storage.personal_kb import is_personal_query as _is_personal_query
from inference.plan_parser import AgentStep, AgentResult, _parse_plan_json, _parse_plan_json_report, _parse_plan, _build_plan_repair_prompt, _DELEGATE_PROMPT_INSTRUCTIONS
from inference.context_builder import ContextBuilder
from inference.saga_manager import SagaManager
from inference.step_executor import StepExecutor




_DEPS_PATTERN = re.compile(
    r"(?:after|deps|depends\s+on)\s*[:=]?\s*([\d,\s&and]+)", re.IGNORECASE
)






# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------















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

    # Wall-clock backstop for a SINGLE step's execution. Deliberately larger than
    # any verb's own internal timeout (RUN_TERMINAL sandbox 60s, git 30s,
    # FETCH_URL 10s) plus the ~10s voice-approval gate, so it never pre-empts a
    # legitimately slow-but-bounded step — it only catches a step with NO internal
    # bound (READ_SCREEN vision inference, a wedged file read on a stalled mount)
    # that would otherwise hold the single dev permit for the scheduler's full
    # 300s plan ceiling, starving every queued dev command. On timeout the step is
    # recorded as a failure and the loop replans instead of wedging.
    STEP_TIMEOUT_S = 180

    # Tighter bound for skill (MCP stdio) calls specifically — the registry has no
    # internal timeout, so an unresponsive skill server would otherwise hang the
    # step for the full STEP_TIMEOUT_S.
    SKILL_CALL_TIMEOUT_S = 30

    # Read-only / idempotent verbs that are safe to retry once on a transient
    # failure. Destructive verbs are NEVER retried (re-running a commit / write /
    # shell command could double-apply) — they go straight to replan-or-halt.
    _RETRYABLE_VERBS: frozenset[str] = frozenset({
        "READ_FILE", "GREP", "READ_SCREEN", "GIT_STATUS", "GIT_DIFF",
        "FETCH_URL", "SEARCH_WEB", "EXPLAIN",
        # DELEGATE is read-only (no side effects) → safe to retry. Deliberately NOT
        # in _PARALLEL_VERBS/_FANOUT_SAFE_VERBS: it spawns a sub-agent, so it runs
        # SEQUENTIALLY (no nested fan-out over one serialized GPU) — spec R1.3.
        "DELEGATE",
    })

    # Verbs whose execution has side effects (writes files, runs shell, mutates
    # git, opens a PR). A plan containing ANY of these is "destructive": its
    # upfront approval, and any per-op confirmation, must fail-safe to DENY on
    # silence / ambiguity / hardware failure — mirroring the hardened voice
    # approval gate (approval_hook.py, timeout_action="reject"). Read-only plans
    # keep their convenient auto-approve-on-silence.
    _DESTRUCTIVE_VERBS: frozenset[str] = frozenset({
        "WRITE_FILE", "EDIT_FILE", "RUN_TERMINAL", "GIT_COMMIT", "GIT_CHECKOUT",
        "GITHUB_PR",
    })

    # Pure context-gathering verbs (gap #1 fan-out): read-only, no side effects,
    # and no inter-step dependency worth serialising. A LEADING run of these in a
    # plan can be executed concurrently via the scheduler's sub-agent pool before
    # the sequential action loop. Subset of _RETRYABLE_VERBS (excludes SEARCH_WEB,
    # which opens a browser, and EXPLAIN, which is ordering-sensitive narration).
    _PARALLEL_VERBS: frozenset[str] = frozenset({
        "READ_FILE", "GREP", "FETCH_URL", "READ_SCREEN", "GIT_STATUS", "GIT_DIFF",
        "SEARCH_PERSONAL",
    })

    # Verbs safe to run CONCURRENTLY within one DAG wave (gap A). Reads (above)
    # plus WRITE_FILE (distinct paths — independence is guaranteed by the absence
    # of a declared dependency) and EXPLAIN (pure narration). Everything else —
    # RUN_TERMINAL, GIT_COMMIT/CHECKOUT, GITHUB_PR, CLICK/OPEN/HOTKEY/TYPE/SCROLL,
    # SEARCH_WEB(browser) — is a BARRIER: it has shared-resource / ordering side
    # effects, so it runs SOLO even when its dependencies are satisfied.
    _FANOUT_SAFE_VERBS: frozenset[str] = _PARALLEL_VERBS | frozenset({
        "WRITE_FILE", "EDIT_FILE", "EXPLAIN",
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
        self._bridge: Optional["BridgeClient"] = None            # set via set_bridge()
        self._scheduler = None                               # set via set_scheduler()
        self._memory = None                                  # set via set_memory()
        self._confirm_whisper = None       # WhisperModel cached for _confirm_destructive_op()
        self._skill_registry = None        # SkillRegistry | None (set via set_skill_registry)
        self._personal_kb = None           # PersonalKB | None (set via set_personal_kb)
        # Edit-format ACI: lint-gates + applies WRITE_FILE edits before they
        # touch disk (specs/edit-format-aci). Stateless; default whole_file.
        self._edit_applier = EditApplier()

        self._context_builder = ContextBuilder(agent=self, agent_db=self._agent_db, memory=self._memory, session_context=session_context)
        self._saga_manager = SagaManager(agent=self, agent_db=self._agent_db)
        self._step_executor = StepExecutor(agent=self, router=self._router, coordinator=self._coordinator, agent_db=self._agent_db)

        # Plan-contract auto-repair (specs/dev-agent-plan-contract). When the
        # planner drops steps (unknown verb) or returns nothing parseable,
        # re-prompt the model with a corrective message instead of silently
        # dropping the step. Default ON (env DA_PLAN_REPAIR) as of 2026-06-24 —
        # the model-free eval baseline is locked (evals/baselines/plan_contract.json,
        # exact_acc=1.0, deterministic repair); set DA_PLAN_REPAIR=0 for byte-identical
        # legacy. Bounded by DA_PLAN_REPAIR_MAX (default 1) so it can't spin.
        # Instance attrs so tests can flip them without env.
        self._plan_repair_enabled: bool = os.environ.get(
            "DA_PLAN_REPAIR", "1").strip().lower() in ("1", "true", "on", "yes")
        try:
            self._plan_repair_max: int = max(
                0, int(os.environ.get("DA_PLAN_REPAIR_MAX", "1")))
        except ValueError:
            self._plan_repair_max = 1

        # Independent code Critic (specs/dev-agent-critic). When ON, a WRITE_FILE
        # edit that passed the lint gate is reviewed by a fresh-context reviewer
        # pass on the already-loaded model BEFORE it commits: a non-pass/low-conf
        # verdict escalates the approval gate; revise/block blocks the write and
        # drives the replan loop. Default ON (DA_CRITIC) as of 2026-06-24 — eval
        # baseline locked (evals/baselines/dev_critic.json, catch_rate=1.0); set
        # DA_CRITIC=0 for byte-identical legacy. No new model loaded (AGENTS.md #6).
        # Tests inject via set_critic().
        self._critic_enabled: bool = os.environ.get(
            "DA_CRITIC", "1").strip().lower() in ("1", "true", "on", "yes")
        try:
            self._critic_confidence_floor: float = float(
                os.environ.get("DA_CRITIC_FLOOR", "0.6"))
        except ValueError:
            self._critic_confidence_floor = 0.6
        try:
            self._critic_max_revisions: int = max(
                0, int(os.environ.get("DA_CRITIC_MAX_REVISIONS", "1")))
        except ValueError:
            self._critic_max_revisions = 1
        self._critic: Optional[Critic] = (
            Critic(router, model_domain=os.environ.get("DA_CRITIC_DOMAIN", "plan"))
            if self._critic_enabled else None
        )
        self._critic_revise_counts: dict[str, int] = {}

        # Autonomous Tester loop (specs/dev-agent-critic R3). When ON, a committed
        # WRITE_FILE to a .py SOURCE file gets a generated pytest test run through
        # the existing sandbox; the outcome is surfaced as an observation on the
        # step result (safe-observation — the good write is never rolled back).
        # Default ON (DA_TESTER) as of 2026-06-24 — gated by the same dev_critic
        # eval baseline; safe-observation (a failing generated test never rolls back
        # the good write). Set DA_TESTER=0 to disable. No new model (code domain
        # already loaded).
        self._tester_enabled: bool = os.environ.get(
            "DA_TESTER", "1").strip().lower() in ("1", "true", "on", "yes")
        self._tester: Optional[Tester] = (
            Tester(router, code_domain=os.environ.get("DA_TESTER_DOMAIN", "code"))
            if self._tester_enabled else None
        )
        # Optional flare/resource gate (R3.6): a callable -> bool; True == skip.
        self._tester_skip_check = None

        # Live repo-context ingestion (specs/repo-context-ingestion, Gap A). When
        # ON, stable workspace facts (AGENTS.md/CLAUDE.md house rules, repo layout,
        # git branch/log) are built ONCE, memoized, and prepended to the plan
        # extra_ctx ahead of the dynamic RAG/git-status context. Default OFF
        # (DA_REPO_CONTEXT) until the eval baseline locks; off == byte-identical.
        # _workspace_block is the memoized block (R2.1); None = not yet built.
        self._repo_context_enabled: bool = os.environ.get(
            "DA_REPO_CONTEXT", "1").strip().lower() in ("1", "true", "on", "yes")
        self._workspace_block: Optional[str] = None
        self._workspace_built: bool = False
        # Repo root for workspace-fact collection (cwd is the repo root in prod —
        # _read_file/_grep/_git_context all assume it). Override in tests.
        self._repo_root: str = os.getcwd()

        # Planner-driven read-only DELEGATE verb (specs/dev-agent-delegate-verb,
        # Gap D). When ON, the planner can emit [DELEGATE <question>] to spin off a
        # bounded read-only investigation sub-agent whose finding returns into the
        # trajectory. Reuses the WorkflowRunner substrate (scheduler sub-agent pool,
        # flare guard, agent_workflows journaling) — no new model (AGENTS.md #6).
        # Default OFF (DA_DELEGATE) until the eval baseline locks; off == the verb is
        # absent from the planner vocabulary and a stray DELEGATE is a safe no-op.
        self._delegate_enabled: bool = os.environ.get(
            "DA_DELEGATE", "1").strip().lower() in ("1", "true", "on", "yes")
        try:
            self._max_delegate_depth: int = max(
                1, int(os.environ.get("DA_DELEGATE_MAX_DEPTH", "1")))
        except ValueError:
            self._max_delegate_depth = 1
        try:
            self._delegate_max_steps: int = max(
                1, int(os.environ.get("DA_DELEGATE_MAX_STEPS", "4")))
        except ValueError:
            self._delegate_max_steps = 4
        try:
            self._delegate_finding_chars: int = max(
                200, int(os.environ.get("DA_DELEGATE_FINDING_CHARS", "1200")))
        except ValueError:
            self._delegate_finding_chars = 1200
        # Current delegation depth (0 = top-level plan); set while investigating so
        # a nested DELEGATE is refused (R3.1). Optional flare/resource skip check.
        self._delegate_depth: int = 0
        self._delegate_skip_check = None   # callable -> bool; True == skip (flare)

        # EventBus — set via set_event_bus(); optional (no-op if None)
        self._event_bus = None

        # Live DAG/token correlation for the PC chat UI. _active_trace_id is the
        # trace_id of the in-flight request (set by handle/plan_and_run); _step_seq
        # maps id(step) → the step's original 1-based plan position so dag.* events
        # and plan.generated agree on node identity (deps reference these indices).
        # Both are safe single-plan state because _plan_lock serializes plans.
        self._active_trace_id: str = ""
        self._step_seq: dict[int, int] = {}
        # Actual model that produced the in-flight plan — resolves the WRITE_FILE
        # edit_format for _apply_edit (specs/edit-format-aci R3). Per-plan state,
        # safe because _plan_lock serializes plans. "" → whole_file default.
        self._active_plan_model: str = ""

        # Goal-level authorization state (reset after each plan)
        self._plan_authorized: bool = False
        self._approved_verbs: frozenset[str] = frozenset()
        self._escalated_this_run: bool = False  # halted plan landed in review queue
        # Saga rollback summary, set by _run_compensations when a rollback runs
        # ({reverted, manual, incomplete, triggered_by}); None when none ran.
        self._rollback_summary: Optional[dict] = None
        # Proactive accessibility notice: speak a short TTS summary when a saga
        # rollback reverts file changes — covers the user-cancel path, which was
        # previously silent about reverting edits (specs/dev-agent-sagas R2.2).
        # Default ON (DA_SAGA_ANNOUNCE); =0 for byte-identical legacy completion speech.
        self._saga_announce: bool = os.environ.get(
            "DA_SAGA_ANNOUNCE", "1").strip().lower() in ("1", "true", "on", "yes")
        # Durable fallback store for escalations the DB can't accept (E4).
        # Instance attr so tests can redirect it off the real home dir.
        self._escalation_sidecar_path: Path = (
            Path.home() / ".claude" / "escalations_pending.jsonl"
        )
        self._confirm_lock: asyncio.Lock = asyncio.Lock()
        # Concurrency guards: plan state (_plan_authorized/_cancel_event/
        # _current_goal/…) is per-plan instance state, so plans must never
        # interleave; the drainer must be single-flight (claim_next_goal's
        # SELECT-then-UPDATE assumes a single consumer).
        self._plan_lock: asyncio.Lock = asyncio.Lock()
        self._drain_lock: asyncio.Lock = asyncio.Lock()
        self._drain_signal: bool = False   # set when drain is requested while one is active
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._current_goal: Optional[str] = None
        self._current_step: int = 0
        self._total_steps: int = 0

    def set_event_bus(self, bus) -> None:
        """Wire the EventBus for publishing replan-exhausted and step-failed events."""
        self._event_bus = bus

    # ---------------------------------------------------------------------- #
    # Live DAG / token event emission (PC chat UI — core/chat_server.py)
    # ---------------------------------------------------------------------- #

    async def _publish_live(self, topic: str, payload: dict) -> None:
        """Best-effort publish of a live-UI event tagged with the active trace_id.

        No-op when no EventBus is wired or no request is in flight, so the dev
        path costs nothing outside a chat-correlated run. Never raises into the
        execution loop.
        """
        if self._event_bus is None or not self._active_trace_id:
            return
        try:
            await self._event_bus.publish(
                topic, payload, source="dev_agent", trace_id=self._active_trace_id
            )
        except Exception as exc:
            log.debug("DevAgent: %s publish failed: %s", topic, exc)

    async def _publish_bg(self, topic: str, payload: dict) -> None:
        """Best-effort publish for BACKGROUND work (no active trace_id required).

        Used by the goal-queue drainer so autonomous goal execution is visible on
        the EventBus (and durable event_log) instead of silent. Never raises.
        """
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(topic, payload, source="dev_agent")
        except Exception as exc:
            log.debug("DevAgent: %s publish failed: %s", topic, exc)

    # Step-card snippet caps (specs/chat-workbench-parity R3.3; spec allows ≤2000).
    _RESULT_SNIPPET_CHARS = 600
    _ARGS_SNIPPET_CHARS = 200

    @classmethod
    def _result_snippet(cls, step: "AgentStep") -> str:
        """A short, secret-safe excerpt for a completed step.

        Never surfaces WRITE_FILE/EDIT_FILE bodies or RUN_TERMINAL stdout (may
        contain secrets) — only a generic status for those verbs; reads/EXPLAIN
        show a truncated result.
        """
        action = step.action.upper()
        if action in ("WRITE_FILE", "EDIT_FILE", "RUN_TERMINAL"):
            return "ok" if step.success else "failed"
        return (step.result or "")[:cls._RESULT_SNIPPET_CHARS]

    @classmethod
    def _args_snippet(cls, step: "AgentStep") -> str:
        """What the step was asked to do — file path / command / query,
        truncated for the chat step card (specs/chat-workbench-parity R3.3)."""
        return (step.args or "")[:cls._ARGS_SNIPPET_CHARS]

    async def _emit_step_started(self, step: "AgentStep") -> None:
        n = self._step_seq.get(id(step))
        if n is None:
            return
        await self._publish_live(TOPIC_DAG_STEP_STARTED, {"n": n, "action": step.action})

    async def _emit_step_completed(self, step: "AgentStep", step_num: int) -> None:
        # Original plan position for mapped steps; completion order for steps a
        # replan injected after plan.generated was published.
        n = self._step_seq.get(id(step), step_num)
        await self._publish_live(TOPIC_DAG_STEP_DONE, {
            "n": n,
            "action": step.action,
            "success": bool(step.success),
            "latency_ms": round(step.latency_ms, 1),
            "result_snippet": self._result_snippet(step),
            "args_snippet": self._args_snippet(step),
        })

    def set_indexer(self, indexer: "CodebaseIndexer") -> None:
        """Wire a CodebaseIndexer for RAG context injection at plan/query time."""
        self._indexer = indexer

    def set_bridge(self, bridge: "BridgeClient") -> None:
        """Wire a BridgeClient for IDE context (cursor, file, git, diagnostics)."""
        self._bridge = bridge

    def set_scheduler(self, scheduler) -> None:
        """Wire AccessibilityScheduler for submitting background sub-tasks at DEV_AGENT priority."""
        self._scheduler = scheduler

    def set_memory(self, memory) -> None:
        """Wire MemoryManager for standardised storage access."""
        self._memory = memory

    def set_skill_registry(self, registry) -> None:
        """Wire the SkillRegistry so SKILL_QUERY/SKILL_CALL steps can run and the
        planner can see available skills."""
        self._skill_registry = registry

    def set_personal_kb(self, kb) -> None:
        """Wire the PersonalKB so personal-document queries and SEARCH_PERSONAL
        plan steps can run."""
        self._personal_kb = kb

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

    def set_repo_root(self, path: str) -> bool:
        self._context_builder.set_repo_root(path)
        self._step_executor.set_repo_root(path)
        """Re-point the DevAgent at a different project (chat active-directory
        switching, specs/chat-context-attachments R1.2). ``_repo_root`` drives
        `_read_file`/`_grep`/`_git_context`/`_workspace_context`. Returns False
        and changes nothing if ``path`` isn't a real directory."""
        import os as _os
        rp = _os.path.realpath(_os.path.expanduser(path or ""))
        if not _os.path.isdir(rp):
            return False
        self._repo_root = rp
        return True

    async def handle(self, text: str, screenshot_b64: Optional[str] = None,
                     trace_id: str = "", attachment_context: str = "") -> AgentResult:
        """Classify, route, and execute a user query.

        - COMMAND domain → passes through to HybridCoordinator (existing pipeline)
        - PLAN domain → plan_and_run loop
        - CODE/MATH/VISION/GENERAL → single specialist inference, result returned

        ``trace_id`` (set by the chat UI via HybridCoordinator) correlates every
        live DAG / token event this request emits to one chat socket. Empty for
        non-chat callers (voice / drain queue) → live emission is a no-op.
        ``attachment_context`` (specs/chat-context-attachments R2.4) is an optional
        per-turn block from extracted file attachments, injected ahead of RAG in
        both the plan and single-turn paths. Empty → byte-identical to today.
        """
        t0 = time.monotonic()
        self._active_trace_id = trace_id
        domain = self._classifier.classify(text)
        log.info("DevAgent: domain=%s  text=%r", domain, text[:80])

        if domain == "command" and self._coordinator:
            # Pass through to the accessibility pipeline
            from core.command_executor import Command
            cmd = Command(text=text, action="CLARIFY", source="voice", trace_id=trace_id)
            result_dict = await self._coordinator.route(cmd)
            return AgentResult(
                goal=text,
                domain="command",
                model_used="llama3.1:8b",
                response_text=str(result_dict),
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        if domain == "skill" and self._skill_registry is not None \
                and self._skill_registry.match_intent(text):
            return await self._handle_skill(text)

        # Personal-document questions ("what did I write in my notes about …")
        # answer from the PersonalKB — never from model hallucination.
        if self._personal_kb is not None and getattr(self._personal_kb, "available", False) \
                and _is_personal_query(text):
            return await self._handle_personal_query(text)

        if domain == "plan":
            return await self.plan_and_run(
                text, trace_id=trace_id, extra_context=attachment_context)

        if domain == "vision" and screenshot_b64 is None:
            # Auto-capture screen for vision queries
            screenshot_b64 = await self._capture_screenshot()

        # Single-turn specialist inference — inject RAG context for dev domains
        extra_ctx = self._format_context()
        if domain in ("code", "math", "vision", "general", "plan"):
            rag = await self._rag_context(text, n=3)
            if rag:
                extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag
        # Per-turn attachment context leads (specs/chat-context-attachments R2.4).
        if attachment_context:
            extra_ctx = (f"{attachment_context}\n\n{extra_ctx}"
                         if extra_ctx else attachment_context)

        # Chat path (trace_id + EventBus wired): stream tokens so the chat UI
        # types the answer out like Claude Code. Each chunk is published as a
        # chat.token event keyed by trace_id; the assembled text becomes the
        # final result. Non-chat callers take the original single-shot path.
        if trace_id and self._event_bus is not None:
            chunks: list[str] = []
            try:
                async for tok in self._router.infer_stream(
                    domain=domain, user_text=text,
                    screenshot_b64=screenshot_b64, context=extra_ctx,
                ):
                    if not tok:
                        continue
                    chunks.append(tok)
                    await self._publish_live(TOPIC_CHAT_TOKEN, {"text": tok})
                full_text = "".join(chunks)
                router_result = RouterResult(
                    text=full_text,
                    model=self._router.select_profile(domain).name,
                    domain=domain,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    free_form=True,
                )
            except Exception as exc:
                log.warning("DevAgent: stream failed (%s) — single-shot fallback", exc)
                router_result = await self._router.infer(
                    domain=domain, user_text=text,
                    screenshot_b64=screenshot_b64, context=extra_ctx,
                )
        else:
            router_result = await self._router.infer(
                domain=domain,
                user_text=text,
                screenshot_b64=screenshot_b64,
                context=extra_ctx,
            )

        # Math answers are always verified against the CAS (SymPy): the model's
        # free-form result is independently recomputed and a one-line verdict is
        # appended. Runs in both the stream and single-shot paths (converged
        # here); for the chat UI the verdict is streamed as a trailing token.
        # Never lets a verification failure break the answer. Opt out with
        # DA_MATH_CAS_VERIFY=0.
        if (domain == "math" and router_result.ok and router_result.text
                and os.environ.get("DA_MATH_CAS_VERIFY", "1") != "0"):
            note = await self._verify_math_with_cas(text, router_result.text)
            if note:
                block = f"\n\n{note}"
                router_result.text += block
                if trace_id and self._event_bus is not None:
                    await self._publish_live(TOPIC_CHAT_TOKEN, {"text": block})

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

    async def _acquire_plan_steps(self, goal, plan_result, extra_ctx):
        """Parse the planner response into steps, auto-repairing dropped/empty
        plans (specs/dev-agent-plan-contract R1).

        Structured parse → regex fallback. When the structured parse dropped a
        step or produced nothing AND the regex fallback didn't rescue a full
        plan, re-prompt the planner up to `_plan_repair_max` times with a
        corrective message naming the failure. Returns `(steps, plan_result)`
        where `plan_result` is the final (possibly repaired) planner response so
        the EXPLAIN fail-safe and `_active_plan_model` reflect what actually ran.
        With repair disabled (default) this is the legacy parse path plus a
        WARNING when steps are silently dropped — never a silent skip.
        """
        attempts = 0
        while True:
            report = _parse_plan_json_report(plan_result.text)
            steps = report.steps
            used_regex = False
            if not report.parsed_ok or not steps:
                regex_steps = _parse_plan(plan_result.text)
                if regex_steps:
                    steps = regex_steps
                    used_regex = True

            need_repair = (
                self._plan_repair_enabled
                and attempts < self._plan_repair_max
                and not used_regex
                and bool(report.dropped or not steps)
            )
            if not need_repair:
                if report.dropped and not used_regex:
                    log.warning(
                        "DevAgent: plan parse dropped %d step(s): %s",
                        len(report.dropped),
                        "; ".join(f"#{d.index} {d.raw_action or d.reason!r}"
                                  for d in report.dropped),
                    )
                return steps, plan_result

            attempts += 1
            log.info("DevAgent: plan auto-repair %d/%d — %d dropped, %d parsed",
                     attempts, self._plan_repair_max, len(report.dropped), len(steps))
            corrective = _build_plan_repair_prompt(report)
            repair_ctx = f"{corrective}\n\n{extra_ctx}" if extra_ctx else corrective
            # The re-infer emits its own inference span (tokens/cost) — R3.2.
            repaired = await self._router.infer(
                domain="plan", user_text=goal, context=repair_ctx)
            if not repaired.ok:
                log.warning("DevAgent: plan auto-repair inference failed (%s) — "
                            "using prior parse", repaired.error)
                return steps, plan_result
            plan_result = repaired

    async def plan_and_run(
        self, goal: str, trace_id: str = "", seed_context: str = "",
        extra_context: str = "",
    ) -> AgentResult:
        """Decompose a complex goal into steps and execute them sequentially.

        Serialized: plan state (_plan_authorized, _cancel_event, _current_goal,
        step counters, GoalSession) is instance-level, so two interleaved plans
        would answer each other's confirmations and un-cancel each other.

        ``trace_id`` (chat UI) correlates plan.generated / dag.* events to one
        socket; empty for non-chat callers → a fresh trace is minted as before.
        ``seed_context`` (specs/resume-working-memory, Gap C) is an optional stable
        block prepended to the plan context — used to seed a resumed plan with what
        the interrupted run already did. Empty → byte-identical to today (R2.2).
        ``extra_context`` (specs/chat-context-attachments R2.4) is an optional
        per-turn block (e.g. extracted file attachments) prepended ahead of all
        other context. Empty → byte-identical to today.
        """
        async with self._plan_lock:
            return await self._plan_and_run_locked(
                goal, trace_id, seed_context, extra_context)

    async def _plan_and_run_locked(
        self, goal: str, cmd_trace_id: str = "", seed_context: str = "",
        turn_context: str = "",
    ) -> AgentResult:
        t0 = time.monotonic()
        log.info("DevAgent: planning goal %r", goal[:80])

        # Unified agent-run trace (gap C): one trace_id spans the whole plan.
        # Setting it as the current ContextVar means every awaited descendant —
        # ModelRouter.infer's inference spans, scheduler.fan_out children — attach
        # to THIS trace automatically, reconstructing the run as one tree. Zero
        # cost when DA_TRACE is off (new_trace returns "" and spans no-op).
        from monitoring.trace import get_tracer
        _tracer = get_tracer()
        # Reuse the chat-supplied trace_id (so live DAG events correlate to the
        # originating socket); otherwise mint a fresh one as before.
        trace_id = cmd_trace_id or _tracer.new_trace(kind="plan", goal=goal[:80])
        self._active_trace_id = trace_id
        _trace_tok = _tracer.set_current(trace_id)
        _tracer.record_span("plan", trace_id=trace_id, goal=goal[:80])

        # Step 1: Generate plan — inject RAG context + git/IDE context
        extra_ctx = self._format_context()
        # Make registered skills available to the planner — data-driven, no
        # per-feature prompt edit. describe_for_prompt() is "" when no skills load.
        if self._skill_registry is not None:
            skills_desc = self._skill_registry.describe_for_prompt()
            if skills_desc:
                extra_ctx = f"{skills_desc}\n\n{extra_ctx}" if extra_ctx else skills_desc
        rag = await self._rag_context(goal, n=4)
        if rag:
            extra_ctx = f"{rag}\n\n{extra_ctx}" if extra_ctx else rag

        # Git context injection (item #8): gives LLM branch/diff awareness
        git_ctx = await self._git_context()
        if git_ctx:
            extra_ctx = f"{git_ctx}\n\n{extra_ctx}" if extra_ctx else git_ctx

        # Live repo-context (Gap A): stable workspace facts (AGENTS.md/CLAUDE.md
        # rules, layout, git branch/log) lead the dynamic RAG/git-status block so
        # the planner sees its house rules first. Memoized; None when off (R3.1,
        # R4.4). The dynamic working-tree diff stays in _git_context above (R3.3).
        workspace = self._workspace_context()
        if workspace:
            extra_ctx = f"{workspace}\n\n{extra_ctx}" if extra_ctx else workspace

        # Resume working-memory (Gap C): a caller-supplied seed block describing what
        # an interrupted run already did. Leads the context so the planner recovers
        # rather than restarting. Empty for the normal (non-resume) path (R2.2).
        #
        # Cross-session memory (R4): a crash-resume seed is the most specific memory,
        # so it wins. ONLY when no caller seed is supplied (a fresh task) do we pull
        # compact memory from recent *related* prior runs — mutually exclusive, so we
        # never double-seed. Flag-gated (DA_SESSION_MEMORY, default OFF); '' otherwise
        # → byte-identical to today.
        if not seed_context:
            seed_context = await self._session_seed_context(goal)
        if seed_context:
            extra_ctx = f"{seed_context}\n\n{extra_ctx}" if extra_ctx else seed_context

        # Per-turn context (specs/chat-context-attachments R2.4): extracted file
        # attachments for THIS message lead all other context so the planner sees
        # them first. Empty for the non-attachment path → byte-identical to today.
        if turn_context:
            extra_ctx = f"{turn_context}\n\n{extra_ctx}" if extra_ctx else turn_context

        # If the plan model uses a structured WRITE_FILE format (hashline/udiff),
        # teach it the format up front so its bodies are edit ops, not whole files
        # (edit-format-aci R3.2 prompt side). Only for those models — whole_file is
        # untouched.
        _plan_fmt = self._router.edit_format_for(self._router.select_profile("plan").name)
        if _plan_fmt == HASHLINE:
            extra_ctx = (
                f"{HASHLINE_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
                if extra_ctx else HASHLINE_PROMPT_INSTRUCTIONS
            )
        elif _plan_fmt == UDIFF:
            extra_ctx = (
                f"{UDIFF_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
                if extra_ctx else UDIFF_PROMPT_INSTRUCTIONS
            )

        # EDIT_FILE (surgical SEARCH/REPLACE) is available to every plan model
        # regardless of its WRITE_FILE knob, so teach the verb unconditionally —
        # the planner should prefer it for targeted changes to existing files and
        # reserve WRITE_FILE for new/whole-file rewrites (specs/edit-format-aci R5).
        extra_ctx = (
            f"{SEARCH_REPLACE_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
            if extra_ctx else SEARCH_REPLACE_PROMPT_INSTRUCTIONS
        )

        # DELEGATE verb (Gap D): only teach it when ON and only at top level (a
        # delegated child must not be told it can delegate — R3.1/R4.4). When off,
        # the planner vocabulary is byte-identical to today.
        if self._delegate_enabled and self._delegate_depth == 0:
            extra_ctx = (
                f"{_DELEGATE_PROMPT_INSTRUCTIONS}\n\n{extra_ctx}"
                if extra_ctx else _DELEGATE_PROMPT_INSTRUCTIONS
            )

        # Assumptions (Gap 1): Ask the planner to explicitly state its assumptions about repo/system state.
        if os.environ.get("DA_PLAN_ASSUMPTIONS", "0").strip().lower() in ("1", "true", "yes", "on"):
            assump = "List any assumptions you are making about the codebase or system state in the `assumptions` array."
            extra_ctx = f"{assump}\n\n{extra_ctx}" if extra_ctx else assump

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
                success=False,
                error=plan_result.error,
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )

        # Record which model produced the plan so WRITE_FILE steps apply the
        # edit format configured for it (specs/edit-format-aci R3.2).
        self._active_plan_model = plan_result.model

        # Prefer structured JSON (Ollama `format` on the plan profile) — it
        # eliminates the free-text body-collision / arg-truncation bugs. Fall
        # back to the regex parser when JSON parsing fails (older Ollama /
        # vLLM / remote backends that don't honor `format`). When auto-repair is
        # enabled (specs/dev-agent-plan-contract), a dropped/empty plan is
        # re-prompted instead of silently degraded; `plan_result` may be replaced
        # by the repaired response.
        steps, plan_result = await self._acquire_plan_steps(goal, plan_result, extra_ctx)
        if not steps:
            # Planner returned neither valid JSON nor a parseable plan, and repair
            # (if any) didn't recover one — fail safe: surface the response as a
            # single read-only EXPLAIN, never a guessed action (R1.5).
            steps = [AgentStep(action="EXPLAIN", body=plan_result.text)]

        log.info("DevAgent: plan has %d steps", len(steps))

        assumptions = []
        if os.environ.get("DA_PLAN_ASSUMPTIONS", "0").strip().lower() in ("1", "true", "yes", "on"):
            try:
                import json
                start = plan_result.text.find("{")
                end = plan_result.text.rfind("}")
                if start != -1 and end != -1:
                    plan_obj = json.loads(plan_result.text[start:end+1])
                    if isinstance(plan_obj, dict):
                        assumptions = plan_obj.get("assumptions", [])
            except Exception:
                pass

        # Upfront plan approval gate: speak summary → voice yes/no.
        # "denied" ABORTS the plan — an explicit "no" (or fail-safe DENY on a
        # destructive plan) must stop every step, not just the three git verbs.
        # "approved" authorizes all steps; "auto" (read-only convenience grant)
        # runs the plan but leaves _plan_authorized False so any destructive
        # step a later replan injects still requires per-op confirmation.
        verdict = await self._approve_plan_upfront(goal, steps, assumptions=assumptions)
        if verdict is True:        # legacy bool contract (tests / older callers)
            verdict = "approved"
        elif verdict is False:
            verdict = "denied"
        if verdict == "denied":
            log.info("DevAgent: plan REJECTED by user — aborting before execution")
            self._reset_plan_state()
            _tracer.record_span("plan_done", trace_id=trace_id, status="rejected")
            _tracer.reset_current(_trace_tok)
            return AgentResult(
                goal=goal, domain="plan",
                model_used=plan_result.model,
                success=False,
                error="Plan rejected by user",
                total_latency_ms=(time.monotonic() - t0) * 1000,
            )
        self._plan_authorized = verdict == "approved"
        self._approved_verbs = frozenset(
            s.action.upper() for s in steps[: self.MAX_STEPS]
        )
        self._cancel_event.clear()
        self._current_goal = goal
        self._total_steps = min(len(steps), self.MAX_STEPS)
        self._current_step = 0
        self._escalated_this_run = False
        self._rollback_summary = None

        # Live DAG: publish the approved plan as a node/edge graph and map each
        # step object to its 1-based plan position so dag.* events line up with
        # the deps edges. No-op when no chat request is in flight.
        _plan_steps = list(steps[: self.MAX_STEPS])
        self._step_seq = {id(s): i for i, s in enumerate(_plan_steps, 1)}
        await self._publish_live(TOPIC_PLAN_GENERATED, {
            "goal": goal[:120],
            "steps": [
                {"n": i, "action": s.action, "args": (s.args or "")[:80], "deps": list(s.deps)}
                for i, s in enumerate(_plan_steps, 1)
            ],
        })

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
        compensated = False   # ensure rollback runs exactly once per terminal path

        # Execution strategy:
        #  - If the planner declared step dependencies AND a scheduler is wired,
        #    run the dependency DAG in waves (gap A) — independent steps run
        #    concurrently. On the first failure / cancellation / unmet dep it
        #    hands the remainder back to the sequential loop below (with replan).
        #  - Otherwise, the proven sequential path runs, after fanning out any
        #    leading read-only context steps (gap #1).
        if self._scheduler is not None and self._plan_has_deps(steps):
            cancelled, dag_failed_step = await self._run_dag_waves(
                remaining, executed, run_id
            )
            # A DAG-wave failure is handled exactly like a sequential step
            # failure: replan from the failure observation. The tail handed back
            # has the failed step's dependents already pruned, so we never run a
            # step whose precondition failed.
            if dag_failed_step is not None and not cancelled:
                recovered = False
                if replans < self.MAX_REPLANS and not self._cancel_event.is_set():
                    replans += 1
                    new_remaining = await self._try_replan(goal, executed, remaining)
                    if new_remaining is not None:
                        remaining = new_remaining
                        self._total_steps = len(executed) + len(remaining)
                        recovered = True
                        log.info("DevAgent: replanned after DAG failure %s — "
                                 "%d new step(s) (replan %d/%d)",
                                 dag_failed_step.action, len(remaining),
                                 replans, self.MAX_REPLANS)
                if not recovered:
                    halted_reason = (
                        f"halted after failed {dag_failed_step.action} (no recovery plan)"
                    )
                    log.warning("DevAgent: %s", halted_reason)
                    await self._halt_and_compensate(
                        run_id, goal, replans, dag_failed_step.action
                    )
                    compensated = True
                    remaining = []
        else:
            await self._gather_readonly_prefix(remaining, executed, run_id)

        while remaining and not cancelled:
            if len(executed) >= self.MAX_STEPS:
                halted_reason = f"reached MAX_STEPS ({self.MAX_STEPS})"
                log.warning("DevAgent: %s", halted_reason)
                # Roll back completed side effects — a partial plan halted by the
                # step cap should not leave half-done destructive work.
                _incomplete = await self._run_compensations(run_id, triggered_by="max_steps")
                await self._record_escalation(run_id, goal, "max_steps", None, replans,
                                              incomplete=_incomplete)
                compensated = True
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

            await self._emit_step_started(step)
            ok = await self._run_step_with_retry(step)
            _tracer.record_span("step", trace_id=trace_id, action=step.action, ok=ok)
            executed.append(step)
            await self._persist_step(run_id, len(executed), step)
            if ok:
                continue

            # Step failed — try a bounded recovery replan; otherwise halt.
            if replans < self.MAX_REPLANS and not self._cancel_event.is_set():
                replans += 1
                new_steps = await self._try_replan(goal, executed, remaining)
                if new_steps is not None:
                    remaining = new_steps
                    self._total_steps = len(executed) + len(remaining)
                    log.info(
                        "DevAgent: replanned after failed %s — %d new step(s) (replan %d/%d)",
                        step.action, len(remaining), replans, self.MAX_REPLANS,
                    )
                    continue
            halted_reason = f"halted after failed {step.action} (no recovery plan)"
            log.warning("DevAgent: %s", halted_reason)
            await self._halt_and_compensate(run_id, goal, replans, step.action)
            compensated = True
            break

        # Cancellation (from the sequential loop above OR from _run_dag_waves)
        # rolls back completed side effects — a cancelled plan must not leave
        # half-done destructive work. Runs once, only if a terminal path above
        # didn't already compensate.
        if cancelled and not compensated:
            # A user cancel is deliberate and does NOT itself escalate. But a
            # compensation that FAILED or was SKIPPED during that rollback (E3/E5)
            # is a durable-integrity problem the human must see — a half-undone
            # destructive plan left in an unknown state. _run_compensations
            # self-escalates each incomplete rollback (reason 'compensation_failed')
            # to the review queue, so the deliberate cancel stays silent while an
            # incomplete rollback still reaches a human, even when the DB is down.
            await self._run_compensations(run_id, triggered_by="user_cancel")
            compensated = True

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
                # Wall-clock backstop: a step with no internal timeout (vision,
                # stalled I/O) can't hold the dev permit until the 300s plan
                # ceiling — it fails here and the loop replans (CancelledError
                # from a real plan cancel is BaseException and propagates).
                step.result = await asyncio.wait_for(
                    self._execute_step(step), timeout=self.STEP_TIMEOUT_S
                )
                step.success = True
                step.latency_ms = (time.monotonic() - step_t0) * 1000
                return True
            except asyncio.TimeoutError:
                step.result = f"ERROR: step timed out after {self.STEP_TIMEOUT_S}s"
                step.success = False
                step.latency_ms = (time.monotonic() - step_t0) * 1000
                log.error(
                    "DevAgent: step %s timed out after %ds (attempt %d/%d)",
                    step.action, self.STEP_TIMEOUT_S, attempt + 1, attempts,
                )
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

    # ── Dependency-DAG wave execution (gap A) ────────────────────────────────

    @staticmethod
    def _plan_has_deps(steps: list[AgentStep]) -> bool:
        """True if the planner declared any inter-step dependency (engages the DAG)."""
        return any(s.deps for s in steps)

    @staticmethod
    def _dependents_closure(pending: dict[int, AgentStep], failed_idx: int) -> set[int]:
        """Indices in `pending` that (transitively) declare `after:` the failed step.

        Those steps' precondition can never be satisfied now, so they must NOT
        run — they're dropped from the deferred tail rather than handed to the
        dep-agnostic sequential drain.
        """
        closure: set[int] = set()
        bad = {failed_idx}
        changed = True
        while changed:
            changed = False
            for i, s in pending.items():
                if i in closure:
                    continue
                if any(d in bad for d in s.deps):
                    closure.add(i)
                    bad.add(i)
                    changed = True
        return closure

    async def _run_dag_waves(
        self,
        remaining: list[AgentStep],
        executed: list[AgentStep],
        run_id: int,
    ) -> tuple[bool, Optional[AgentStep]]:
        """Execute a dependency-ordered plan in waves, fanning out independent steps.

        Each wave runs every step whose declared deps are already satisfied:
        fan-out-safe steps (reads / WRITE_FILE / EXPLAIN) run CONCURRENTLY via the
        scheduler's sub-agent pool; barrier steps (RUN_TERMINAL, git, UI, …) run
        SOLO. Steps are 1-based by their original plan position (what `deps`
        reference).

        Stops on the first failure, on a dependency cycle / dep-on-failed-step
        (no ready steps), or on cancellation. On a failure it DROPS the failed
        step's transitive dependents from the tail (their precondition is gone)
        and returns the failed step so the caller routes into the replan path —
        a failed step's dependents must never blindly run. Returns
        (cancelled, failed_step|None). Mutates `executed` (append, in completion
        order) and `remaining` (the not-completed, still-runnable tail).
        """
        # 1-based position → step, preserving the planner's numbering for deps.
        pending: dict[int, AgentStep] = {i: s for i, s in enumerate(remaining, 1)}
        completed: set[int] = set()
        cancelled = False
        failed_step: Optional[AgentStep] = None
        failed_idx: Optional[int] = None

        while pending and failed_step is None and not cancelled:
            if self._cancel_event.is_set():
                cancelled = True
                break
            ready = [i for i, s in pending.items()
                     if all(d in completed for d in s.deps)]
            if not ready:
                # Cycle, or a dependency landed on a step that didn't complete —
                # let the sequential loop sort out the remainder. Log the specific
                # unmet deps per pending step (not just the count) so a stuck DAG
                # is diagnosable instead of silently degrading (E19).
                unmet = {i: sorted(set(s.deps) - completed) for i, s in pending.items()}
                log.warning("DevAgent[dag]: no ready steps (cycle/unmet dep) — "
                            "%d step(s) deferred to sequential; unmet deps %r",
                            len(pending), unmet)
                break

            safe = [i for i in ready if pending[i].action.upper() in self._FANOUT_SAFE_VERBS]
            barriers = [i for i in ready if i not in safe]

            # De-collide same-path WRITE_FILE/EDIT_FILE within the concurrent
            # batch (#14). The planner's "distinct paths" independence claim is
            # unverified; two concurrent writes to one path race (nondeterministic
            # last-writer + racing saga snapshots). Keep the lowest-indexed writer
            # per path in the fan-out; demote later same-path writers to serial
            # barriers so they run one-at-a-time in plan order.
            seen_write_paths: set[str] = set()
            deduped_safe: list[int] = []
            for i in sorted(safe):
                s = pending[i]
                if s.action.upper() in ("WRITE_FILE", "EDIT_FILE"):
                    p = os.path.normcase(os.path.normpath((s.args or "").strip()))
                    if p in seen_write_paths:
                        barriers.append(i)
                        continue
                    seen_write_paths.add(p)
                deduped_safe.append(i)
            safe = deduped_safe

            # Live DAG: every step in this concurrent batch lights up together.
            for i in safe:
                await self._emit_step_started(pending[i])
            # Fan-out-safe ready steps run concurrently (or inline if just one).
            if len(safe) >= 2 and self._scheduler is not None:
                results = await self._scheduler.fan_out(
                    [self._run_step_with_retry(pending[i]) for i in safe],
                    label=f"dag_wave[{len(safe)}]",
                )
            else:
                results = [await self._run_step_with_retry(pending[i]) for i in safe]

            for idx, ok in zip(safe, results):
                step = pending.pop(idx)
                executed.append(step)
                await self._persist_step(run_id, len(executed), step)
                if ok is True:
                    completed.add(idx)
                elif failed_step is None:
                    failed_step, failed_idx = step, idx
            if failed_step is not None:
                break

            # Barriers run one at a time, in plan order.
            for idx in sorted(barriers):
                if self._cancel_event.is_set():
                    cancelled = True
                    break
                await self._emit_step_started(pending[idx])
                ok = await self._run_step_with_retry(pending[idx])
                step = pending.pop(idx)
                executed.append(step)
                await self._persist_step(run_id, len(executed), step)
                if ok is True:
                    completed.add(idx)
                else:
                    failed_step, failed_idx = step, idx
                    break

        # On failure, drop the failed step's transitive dependents — they can
        # never legally run. Survivors stay in the tail as replan context.
        dropped: set[int] = set()
        if failed_idx is not None:
            dropped = self._dependents_closure(pending, failed_idx)
            if dropped:
                log.info("DevAgent[dag]: dropping %d dependent(s) of failed step %d",
                         len(dropped), failed_idx)
        tail = [pending[i] for i in sorted(pending) if i not in dropped]
        remaining[:] = tail
        log.info("DevAgent[dag]: completed %d step(s) in waves, %d deferred%s",
                 len(completed), len(tail), " (cancelled)" if cancelled else
                 (" (failure → replan)" if failed_step is not None else ""))
        return cancelled, failed_step

    @classmethod
    def build_replan_prompt(
        cls, goal: str, executed: list[AgentStep], remaining: list[AgentStep],
        *, enabled: bool = False,
    ) -> tuple[str, dict]:
        """Build the recovery-replan user prompt + return (prompt, traj_stats).

        Extracted so the replan eval (`evals.run --mode replan`) scores the EXACT
        prompt production sends — a true closed loop, not a copy. `enabled` gates
        trajectory reduction (`inference/trajectory.render_trajectory`): False
        reproduces the legacy per-step rendering byte-for-byte.
        """
        from inference.trajectory import render_trajectory, dedup_enabled
        traj_text, traj_stats = render_trajectory(
            executed, style="replan",
            readonly_verbs=cls._PARALLEL_VERBS, enabled=enabled,
            dedup_reads=dedup_enabled(),
        )
        lines = [f"Goal: {goal}", "", "Steps already executed (with outcomes):", traj_text]
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
        return "\n".join(lines), traj_stats

    async def _replan(
        self, goal: str, executed: list[AgentStep], remaining: list[AgentStep]
    ) -> list[AgentStep]:
        """Ask the planner for a revised plan for the REMAINING work after a failure.

        Feeds the executed steps + their outcomes (the observation signal) back to
        the plan-domain model so it can recover. Returns parsed steps, or [] if the
        planner errors or declines.
        """
        # Synthesize the executed trajectory before re-feeding it (token economics —
        # spec specs/trajectory-reduction/). enabled=False reproduces the legacy
        # per-step rendering byte-for-byte; the flag (DA_TRAJECTORY_REDUCE) gates
        # the reduction until the eval baseline locks.
        from inference.trajectory import reduction_enabled
        _reduce = reduction_enabled()
        prompt, traj_stats = self.build_replan_prompt(
            goal, executed, remaining, enabled=_reduce
        )
        if _reduce and traj_stats["chars_saved"] > 0:
            try:
                from monitoring.trace import get_tracer
                get_tracer().record_span(
                    "replan",
                    traj_steps_in=traj_stats["steps_in"],
                    traj_steps_rendered=traj_stats["lines_out"],
                    traj_chars_saved=traj_stats["chars_saved"],
                )
            except Exception:
                pass
        try:
            r = await self._router.infer(domain="plan", user_text=prompt, context=None)
            if r.ok and r.text:
                try:
                    steps = _parse_plan_json(r.text)
                except Exception:
                    steps = []
                if not steps:
                    steps = _parse_plan(r.text)
                
                # Gap 2: Replan Critic
                if steps and os.environ.get("DA_REPLAN_CRITIC", "0").strip().lower() in ("1", "true", "yes", "on"):
                    try:
                        from inference.critic import Critic
                        critic = Critic(self._router, model_domain="plan")
                        verdict = await critic.review_plan(goal, r.text)
                        if verdict.decision in ("revise", "block"):
                            log.warning("DevAgent: replan rejected by Critic: %s", verdict.summary())
                            msg = f"Replan rejected by Critic ({verdict.decision}):\n"
                            for f in verdict.findings:
                                msg += f"- [{f.severity}] {f.message}\n"
                            if verdict.suggested_fix:
                                msg += f"\nSuggestion: {verdict.suggested_fix}"
                            # Return a synthetic step to inject the critic's findings as an observation
                            return [AgentStep(action="CRITIC_REJECT", body=msg)]
                    except Exception as e:
                        log.debug("DevAgent._replan critic check failed: %s", e)

                return steps
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


    # Max file size we snapshot for a WRITE_FILE rollback. Above this, we record
    # that the file existed (so rollback won't delete it) but keep no backup.
    _SAGA_SNAPSHOT_MAX_BYTES = 256 * 1024









    async def _try_replan(
        self, goal: str, executed: list[AgentStep], remaining: list[AgentStep]
    ) -> Optional[list[AgentStep]]:
        """One bounded recovery replan. Returns the new (budget-capped) remaining
        steps, or None if the planner declined/errored (caller should halt).

        Recomputes destructiveness: the upfront approval covered the ORIGINAL
        plan's verbs, so a replan that injects a destructive verb the user never
        heard described revokes the blanket authorization — that step (and every
        later destructive step) then goes through per-op confirmation.
        """
        new_steps = await self._replan(goal, executed, remaining)
        
        # Handle Critic rejection: feed it back as an error observation and recursively replan
        if new_steps and new_steps[0].action == "CRITIC_REJECT":
            rejection_step = AgentStep(action="PLAN", body="Proposed recovery plan")
            rejection_step.step_num = len(executed) + 1
            executed.append(rejection_step)
            # Create a synthetic result for the rejected plan
            from inference.dev_agent import AgentResult
            critic_res = AgentResult(goal=goal, domain="plan", success=False, error=new_steps[0].body)
            self._observations.record(rejection_step, critic_res)
            # Return empty so the caller's loop will try replanning again if budget allows
            return []

        # S2.5 / E18: Planner honesty. A replan that yields only EXPLAIN steps
        # means it cannot proceed. Filter them out so it parses to zero real steps.
        # This will return None and properly halt the plan instead of a false success.
        real_steps = [s for s in new_steps if s.action != "EXPLAIN"]
        if not real_steps:
            return None
        injected = {
            s.action.upper() for s in new_steps
        } & self._DESTRUCTIVE_VERBS - self._approved_verbs
        if injected and self._plan_authorized:
            log.warning(
                "DevAgent: replan injected unapproved destructive verb(s) %s — "
                "revoking blanket plan authorization", sorted(injected),
            )
            self._plan_authorized = False
        budget = max(0, self.MAX_STEPS - len(executed))
        return new_steps[:budget]











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
        runs = await db.runs.get_interrupted_runs(limit=1)
        if not runs:
            return None
        run = runs[0]
        goal = run.get("goal", "")
        if not await self._confirm_destructive_op(f"Resume the interrupted task: {goal[:60]}?"):
            log.info("DevAgent.resume_pending_plan: user declined resume of run %s", run.get("id"))
            # Declining a resume rolls back the crashed run's completed side
            # effects — the user chose not to finish it, so partial destructive
            # work shouldn't be left behind.
            run_id = run.get("id")
            if run_id is not None:
                await self._run_compensations(int(run_id), triggered_by="user_cancel")
            return None
        log.info("DevAgent.resume_pending_plan: resuming run %s — %r", run.get("id"), goal[:60])
        # Working-memory seed (Gap C): derive what the interrupted run already did
        # from its persisted steps and seed the resumed plan with it, so the planner
        # recovers instead of restarting blind. Flag-gated (DA_RESUME_MEMORY) and
        # degrades to an empty seed (today's behavior) on any failure (R2, R3.2).
        seed = await self._resume_seed_context(run.get("id"), goal, run=run)
        await self.plan_and_run(goal, seed_context=seed)
        return run

    async def _resume_seed_context(self, run_id, goal: str, run: dict = None) -> str:
        """Build the resume working-memory seed block, or '' (Gap C, R2/R3).

        Off (DA_RESUME_MEMORY unset) or any failure → '' so resume is byte-identical
        to today. Derived from the durable agent_steps — no schema change (R3.1)."""
        from inference.working_memory import memory_enabled
        if not memory_enabled() or run_id is None:
            return ""
        try:
            from inference.working_memory import summarize_run, render_seed
            db = self._db()
            steps = await db.runs.get_steps_for_run(int(run_id))
            if not steps:
                return ""
                
            run_end_ts = 0.0
            if run and "ts" in run:
                run_end_ts = run["ts"] + sum(s.get("latency_ms", 0) for s in steps) / 1000.0
                
            return render_seed(summarize_run(goal, steps, run_end_ts=run_end_ts))
        except Exception as exc:
            log.debug("DevAgent._resume_seed_context failed: %s", exc)
            return ""


    # ---------------------------------------------------------------------- #
    # Planner-driven DELEGATE — bounded read-only sub-agent (Gap D)
    # ---------------------------------------------------------------------- #

    def _delegate_should_skip_flare(self) -> bool:
        """True if a flare is active (AGENTS.md #5) — investigation is non-essential
        heavy work. A skip-check that errors fails safe to SKIP."""
        if self._delegate_skip_check is None:
            return False
        try:
            return bool(self._delegate_skip_check())
        except Exception:
            return True

    async def _delegate_investigate(self, question: str, depth: int) -> str:
        """Run a bounded, READ-ONLY investigation sub-agent and return its finding.

        Reuses the WorkflowRunner substrate (scheduler sub-agent pool, flare guard,
        agent_workflows journaling); the child runs a small plan→execute loop
        restricted to read-only verbs (never re-entering plan_and_run / _plan_lock,
        R3.2). Always returns a safe observation string — never raises into the
        parent's _execute_step (R4.3).
        """
        if not self._delegate_enabled:
            return "DELEGATE skipped: feature disabled"
        if not question:
            return "DELEGATE skipped: empty question"
        if depth > self._max_delegate_depth:        # no recursion / fan-bomb (R3.1)
            return "DELEGATE refused: max delegation depth"
        if self._delegate_should_skip_flare():      # AGENTS.md #5 (R4.2)
            await self._journal_delegate(question, 0, 0, "skipped_flare")
            return "DELEGATE skipped: flare"

        async def _run() -> str:
            return await self._delegate_loop(question, depth)

        try:
            if self._scheduler is not None and hasattr(self._scheduler, "fan_out"):
                # Run under the sub-agent semaphore, not the dev permit (R3.2).
                results = await self._scheduler.fan_out([_run()], label="delegate")
                r = results[0] if results else None
                if isinstance(r, BaseException) or r is None:
                    raise r if isinstance(r, BaseException) else RuntimeError("no result")
                return r
            return await _run()
        except Exception as exc:                    # safe observation, never raise (R4.3)
            log.warning("DevAgent._delegate_investigate(%r) failed: %s", question[:60], exc)
            await self._journal_delegate(question, 0, 0, "error", error=str(exc))
            return f"DELEGATE failed: {exc}"

    async def _delegate_loop(self, question: str, depth: int) -> str:
        """The bounded read-only investigation itself (no _plan_lock). Plan →
        execute read-only steps (allowlist-enforced) → synthesize a finding."""
        # Scoped context: the question + any RAG hits for it. The child inherits
        # *enough to help*, not the parent's full trajectory (bounded payload).
        rag = await self._rag_context(question, n=3)
        child_ctx = (
            "You are a READ-ONLY investigator. Answer the question using ONLY these "
            "verbs: READ_FILE, GREP, FETCH_URL, READ_SCREEN, GIT_STATUS, GIT_DIFF, "
            "SEARCH_PERSONAL. You may NOT write files, run shell, or take any action. "
            f"Produce at most {self._delegate_max_steps} steps in the [ACTION args] "
            "format, then stop."
        )
        if rag:
            child_ctx = f"{rag}\n\n{child_ctx}"

        plan_result = await self._router.infer(
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
        steps = steps[: self._delegate_max_steps]

        observations: list[str] = []
        ran = 0
        prev_depth = self._delegate_depth
        self._delegate_depth = depth        # so a nested DELEGATE is refused (R3.1)
        try:
            for s in steps:
                act = s.action.upper()
                if act not in self._PARALLEL_VERBS:
                    # Deny-by-default: a child step naming any non-read-only verb is
                    # DROPPED, never executed (R2.1). Structurally read-only.
                    log.info("DevAgent.delegate: dropped non-read-only child step %s", act)
                    continue
                try:
                    res = await asyncio.wait_for(
                        self._execute_step(s), timeout=self.STEP_TIMEOUT_S)
                    observations.append(f"[{act} {s.args[:60]}]\n{(res or '')[:600]}")
                    ran += 1
                except Exception as exc:
                    observations.append(f"[{act} {s.args[:60]}] failed: {exc}")
        finally:
            self._delegate_depth = prev_depth

        if not observations:
            await self._journal_delegate(question, len(steps), 0, "completed")
            return f"DELEGATE finding: no read-only evidence gathered for: {question}"

        synth = await self._router.infer(
            domain="plan",
            user_text=(
                f"Question: {question}\n\nRead-only observations:\n"
                + "\n\n".join(observations)
                + "\n\nAnswer the question concisely from the observations only."
            ),
            context="",
        )
        finding = (getattr(synth, "text", "") or "").strip()[: self._delegate_finding_chars]
        await self._journal_delegate(question, len(steps), ran, "completed")
        return f"DELEGATE finding: {finding}" if finding else \
            f"DELEGATE finding: gathered {ran} observation(s) for: {question}"

    async def _journal_delegate(
        self, question: str, subtask_count: int, success_count: int,
        status: str, error: Optional[str] = None,
    ) -> None:
        """Best-effort journal to the existing agent_workflows ledger (mode=
        'delegate', R4.1) — a DB failure never breaks the investigation."""
        db = self._agent_db
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

    async def drain_goal_queue(self, max_goals: int = 0) -> int:
        """Drain the durable goal backlog (gap D): claim → run → mark terminal.

        Single-flight: claim_next_goal's SELECT-then-guarded-UPDATE is only
        race-safe with one consumer, and concurrent drainers would interleave
        plan state. If a drain is already active (startup drain still running
        when a voice "authorize" enqueues a new goal), this call signals the
        active drainer to re-check the queue after it thinks it's empty, and
        returns 0 — the goal is never abandoned.

        Flare gate: before each claim, waits on the scheduler's dev-admission
        event so no NEW heavy plan starts mid-flare (the real production
        enforcement of pause_dev for this path).

        Stops when the queue is empty, on cancellation, or after `max_goals`
        (0 = until empty). Returns the number processed. Each goal's outcome is
        persisted, so this is safe to call again at any time (e.g. after a
        crash — see AgentDB.requeue_stale_running).
        """
        db = self._db()
        if not db or not getattr(db, "available", False):
            return 0
        if self._drain_lock.locked():
            # An active drainer exists — tell it to re-check after its final
            # empty claim so a goal enqueued in that window isn't stranded.
            self._drain_signal = True
            log.info("DevAgent.drain_goal_queue: drain already active — signalled re-check")
            return 0
        async with self._drain_lock:
            processed = 0
            while True:
                self._drain_signal = False
                while not (max_goals and processed >= max_goals):
                    if self._cancel_event.is_set():
                        break
                    # Flare admission gate: never START a heavy plan mid-flare.
                    sched = self._scheduler
                    if sched is not None and getattr(sched, "dev_paused", False):
                        log.info(
                            "DevAgent.drain_goal_queue: dev admission paused "
                            "(flare) — waiting before next claim"
                        )
                        await sched.wait_dev_admission()
                    goal = await db.goals.claim_next_goal()
                    if goal is None:
                        break
                    gid = int(goal["id"])
                    log.info("DevAgent.drain_goal_queue: running goal %s — %r",
                             gid, goal["goal"][:60])
                    await self._publish_bg(TOPIC_GOAL_DEQUEUED, {
                        "goal_id": gid,
                        "goal": goal["goal"][:200],
                        "source_trigger": goal.get("source_trigger"),
                    })
                    _g_status, _g_ok = "failed", False
                    try:
                        result = await self.plan_and_run(goal["goal"])
                        _g_ok = bool(result.success)
                        _g_status = "done" if _g_ok else "failed"
                        await db.goals.complete_goal(gid, _g_status, error=result.error)
                    except Exception as exc:
                        log.error("DevAgent.drain_goal_queue: goal %s raised: %s", gid, exc)
                        _g_status, _g_ok = "failed", False
                        await db.goals.complete_goal(gid, "failed", error=str(exc))
                    await self._publish_bg(TOPIC_GOAL_COMPLETED, {
                        "goal_id": gid, "status": _g_status, "success": _g_ok,
                    })
                    processed += 1
                # Re-check once if another caller requested a drain while we ran
                # (until-empty mode only; bounded calls return at their cap).
                if not self._drain_signal or max_goals:
                    break
            if processed:
                log.info("DevAgent.drain_goal_queue: processed %d goal(s)", processed)
            return processed

    # ---------------------------------------------------------------------- #
    # Plan-level authorization helpers
    # ---------------------------------------------------------------------- #

    async def _approve_plan_upfront(self, goal: str, steps: list[AgentStep], assumptions: list[str] = None) -> str:
        """Speak plan summary, capture voice yes/no, write GoalSession on approval.

        Returns a verdict string consumed by plan_and_run:
          - "approved" — explicit spoken yes; all steps (incl. destructive) run
            without per-op confirmation.
          - "denied"   — explicit spoken no, or fail-safe DENY on a destructive
            plan (silence / ambiguity / hardware failure). The plan must ABORT.
          - "auto"     — read-only plan with no clear consent (hardware failure /
            silence): runs for convenience, but WITHOUT blanket authorization,
            so any destructive step a replan later injects still confirms per-op.
        """
        from core.goal_session import GoalSessionStore

        verbs = [s.action for s in steps[: self.MAX_STEPS]]
        verb_summary = ", ".join(verbs[:6])
        if len(verbs) > 6:
            verb_summary += f" … (+{len(verbs) - 6} more)"
        n = min(len(steps), self.MAX_STEPS)
        message = f"I'll run {n} step{'s' if n != 1 else ''}: {verb_summary}. Approve all?"

        if os.environ.get("DA_PLAN_PREVIEW", "0").strip().lower() in ("1", "true", "yes", "on"):
            threshold = int(os.environ.get("DA_PLAN_PREVIEW_THRESHOLD", "3"))
            if len(steps) >= threshold:
                try:
                    actions = []
                    for s in steps[:self.MAX_STEPS]:
                        if s.action:
                            args_trunc = str(s.args)[:80] if s.args else ""
                            actions.append(f"Step {s.step_num}: {s.action} {args_trunc}")
                    assumptions_text = ""
                    if assumptions:
                        assumptions_text = "Assumptions made by planner:\n" + "\n".join(f"- {a}" for a in assumptions) + "\n\n"
                    prompt = (
                        f"Goal: {goal}\n"
                        f"{assumptions_text}"
                        f"Steps proposed:\n" + "\n".join(actions) + "\n\n"
                        "Provide a 1-sentence plain English spoken summary of what this plan intends to do. "
                        "Do NOT list the API verbs (like write_file). Just summarize the outcome."
                    )
                    res = await asyncio.wait_for(
                        self._router.infer(domain="plan", user_text=prompt, context=None),
                        timeout=5.0
                    )
                    if res and res.ok and res.text:
                        preview = res.text.strip()
                        if preview.startswith('"') and preview.endswith('"'):
                            preview = preview[1:-1]
                        if preview:
                            message = f"{preview} Approve all?"
                except Exception as exc:
                    log.warning("DevAgent._approve_plan_upfront: preview generation failed: %s", exc)

        log.info("DevAgent: requesting plan approval — %s", message)

        plan_is_destructive = any(
            s.action.upper() in self._DESTRUCTIVE_VERBS for s in steps[: self.MAX_STEPS]
        )

        # Live UI: surface an approval card in the chat (the spoken question +
        # whether it's destructive). The actual yes/no still flows through the
        # shared ~/.claude/approval signal files below — the chat just becomes
        # another responder. No-op when no chat request is in flight.
        # The proposed steps ride along so the chat renders a reviewable plan-
        # preview card instead of a bare question (specs/chat-workbench-parity
        # R5/R6) — additive payload; old clients ignore the extra keys.
        await self._publish_live(TOPIC_DAG_APPROVAL, {
            "message": message, "destructive": plan_is_destructive,
            "goal": goal[:200],
            "steps": [
                {"n": s.step_num or i, "action": s.action,
                 "args": (s.args or "")[:self._ARGS_SNIPPET_CHARS]}
                for i, s in enumerate(steps[: self.MAX_STEPS], 1)
            ],
        })

        def _grant(verdict: str) -> str:
            GoalSessionStore.create(goal=goal, domain="plan")
            return verdict

        def _fallback(reason: str) -> str:
            """No clear consent obtained (hardware failure / silence / ambiguity).

            Read-only plans auto-run for convenience (but WITHOUT blanket
            authorization — see "auto" in the docstring); destructive plans
            fail-safe to DENY — never run side effects without an explicit yes.
            """
            if plan_is_destructive:
                log.info(
                    "DevAgent._approve_plan_upfront: %s + destructive plan → DENY", reason
                )
                return "denied"
            log.info(
                "DevAgent._approve_plan_upfront: %s + read-only plan → auto-run", reason
            )
            return _grant("auto")

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
                    self._confirm_whisper = await asyncio.to_thread(
                        WhisperModel, "tiny", device="cpu", compute_type="int8"
                    )

                def _transcribe() -> str:
                    segs, _ = self._confirm_whisper.transcribe(
                        audio, language="en", beam_size=1, vad_filter=False
                    )
                    return " ".join(s.text for s in segs).lower().strip()

                transcript = await asyncio.to_thread(_transcribe)
            except Exception as exc:
                return _fallback(f"mic fallback failed ({exc})")

        # Shared confirmation vocabulary (core/approval_keywords). An explicit
        # deny always blocks. An explicit yes grants. Anything else (ambiguous /
        # unrecognised) defers to _fallback: auto-approve for read-only plans,
        # fail-safe DENY for destructive ones.
        verdict = classify_confirmation(transcript)
        if verdict == "deny":
            log.info("DevAgent._approve_plan_upfront: REJECTED — %r", transcript)
            return "denied"
        if verdict == "approve":
            log.info("DevAgent._approve_plan_upfront: approved — %r", transcript)
            return _grant("approved")
        return _fallback(f"ambiguous reply {transcript!r}")

    async def _speak_plan_completion(self, result: AgentResult, cancelled: bool) -> None:
        """Speak a short TTS summary after a plan finishes."""
        if cancelled:
            msg = (f"Task cancelled at step {self._current_step} of {self._total_steps}.")
            # A cancel that rolled back edits used to be silent about it (R2.2).
            msg += self._rollback_notice()
        elif result.success:
            spoken_msg = await self._generate_walkthrough(result)
            if spoken_msg:
                msg = spoken_msg
            else:
                summary = (result.response_text or "")[:80].replace("\n", " ")
                msg = f"Done. {summary}" if summary else "Plan complete."
        else:
            failed = [s for s in result.steps if not s.success]
            first_err = (failed[0].result or "")[:60] if failed else ""
            msg = f"Task failed at step {self._current_step}: {first_err}" if first_err else "Plan failed."
            if self._escalated_this_run:
                msg += " Changes rolled back and saved to the review queue."
            else:
                # Halt path that rolled back but didn't escalate (e.g. escalation
                # persist failed) — still tell the user the edits were reverted.
                msg += self._rollback_notice()
        try:
            from tts.polly_stream import get_client as _get_tts
            asyncio.create_task(_get_tts().speak(msg))
        except Exception as exc:
            log.debug("DevAgent._speak_plan_completion: TTS failed: %s", exc)

    async def _generate_walkthrough(self, result: AgentResult) -> Optional[str]:
        if os.environ.get("DA_POST_RUN_WALKTHROUGH", "0").strip().lower() not in ("1", "true", "yes", "on"):
            return None
            
        try:
            actions = []
            for s in result.steps:
                if s.action:
                    args_trunc = str(s.args)[:80] if s.args else ""
                    actions.append(f"Step {s.step_num}: {s.action} {args_trunc}")
            
            prompt = (
                f"Goal: {self._current_goal}\n"
                f"Steps taken:\n" + "\n".join(actions) + "\n\n"
                "Write a markdown walkthrough summarizing the changes made.\n"
                "Also, provide a 1-sentence spoken summary wrapped in <spoken> tags."
            )
            
            res = await asyncio.wait_for(
                self._router.infer(domain="plan", user_text=prompt, context=None),
                timeout=15.0
            )
            
            if res and res.ok and res.text:
                text = res.text
                spoken_msg = None
                
                if "<spoken>" in text and "</spoken>" in text:
                    start = text.find("<spoken>") + 8
                    end = text.find("</spoken>")
                    spoken_msg = text[start:end].strip()
                    text = text[:text.find("<spoken>")] + text[end+9:]
                
                with open("walkthrough.md", "w", encoding="utf-8") as f:
                    f.write(text.strip())

                # Chat artifact card (specs/chat-workbench-parity R8.1): surface
                # the walkthrough markdown in the transcript, not just TTS+disk.
                # No-op for non-chat runs (_publish_live gates on trace_id).
                await self._publish_live(TOPIC_DAG_WALKTHROUGH,
                                         {"markdown": text.strip()})

                return spoken_msg
        except Exception as exc:
            log.warning("DevAgent._generate_walkthrough failed: %s", exc)
            
        return None

    def _rollback_notice(self) -> str:
        """Spoken addendum describing a saga rollback (DA_SAGA_ANNOUNCE).

        Returns '' when the flag is off or no rollback ran, so completion speech is
        byte-identical to legacy in those cases (specs/dev-agent-sagas R2.2). The
        counts come from _run_compensations' self._rollback_summary."""
        if not self._saga_announce:
            return ""
        rb = self._rollback_summary
        if not rb:
            return ""
        reverted = rb.get("reverted", 0)
        manual = rb.get("manual", 0)
        incomplete = rb.get("incomplete", 0)
        parts: list[str] = []
        if reverted:
            parts.append(f"Reverted {reverted} file change{'' if reverted == 1 else 's'}.")
        if manual:
            parts.append(
                f"{manual} terminal action{'' if manual == 1 else 's'} need manual review.")
        if incomplete:
            parts.append(
                f"{incomplete} change{'' if incomplete == 1 else 's'} could not be rolled back.")
        return (" " + " ".join(parts)) if parts else ""

    def _reset_plan_state(self) -> None:
        """Clean up goal-session and status fields after a plan run."""
        from core.goal_session import GoalSessionStore
        GoalSessionStore.cancel()
        self._plan_authorized = False
        self._approved_verbs = frozenset()
        self._escalated_this_run = False
        self._rollback_summary = None
        self._cancel_event.clear()
        self._current_goal = None
        self._current_step = 0
        self._total_steps = 0
        self._active_plan_model = ""
        self._critic_revise_counts = {}

    def set_critic(self, critic: Optional[Critic], *, enabled: bool = True) -> None:
        """Wire (or replace) the Critic and toggle it. Used by main.py and tests."""
        self._critic = critic
        self._critic_enabled = bool(enabled and critic is not None)

    def set_tester(self, tester: Optional[Tester], *, enabled: bool = True,
                   skip_check=None) -> None:
        """Wire (or replace) the Tester. `skip_check` is an optional callable -> bool
        (True == skip, e.g. on a pain-day flare / VRAM eviction — R3.6)."""
        self._tester = tester
        self._tester_enabled = bool(enabled and tester is not None)
        if skip_check is not None:
            self._tester_skip_check = skip_check

    def _tester_should_skip(self) -> bool:
        """Best-effort flare/resource gate (R3.6). Defaults to never-skip; a wired
        check that itself errors fails safe to SKIP (test-gen is non-essential)."""
        if self._tester_skip_check is None:
            return False
        try:
            return bool(self._tester_skip_check())
        except Exception:
            return True

    async def _maybe_run_tester(self, step: AgentStep, write_result: str) -> str:
        """After a committed WRITE_FILE, optionally generate + run a test and append
        its outcome to the step result as an observation (specs/dev-agent-critic R3).

        Default OFF → returns `write_result` unchanged. Only fires for `.py` source
        files; never raises, never blocks, never reports a skip as a pass.
        """
        if self._tester is None or not self._tester_enabled:
            return write_result
        target = (step.args or "").strip()
        if not is_testable_source(target):
            return write_result
        if self._tester_should_skip():
            log.info("DevAgent: tester skipped (flare/resource) — %s", target)
            return write_result
        try:
            code = await asyncio.to_thread(self._read_current_for_critic, target)
            outcome = await self._tester.generate_and_run(
                goal=self._current_goal or "", path=target, code=code)
        except Exception as exc:
            log.warning("DevAgent: tester failed (%s) — skipped", exc)
            return write_result
        if outcome.note:
            return f"{write_result}\n{outcome.note}"
        return write_result

    async def _critic_review(self, step: AgentStep, new_text: str) -> CriticVerdict:
        """Independent review of a lint-passed WRITE_FILE edit (specs/dev-agent-critic).

        Default OFF → an immediate PASS (no model call), so the WRITE_FILE path is
        byte-identical to legacy. When ON: reviews the diff on the already-loaded
        model with a fresh reviewer context; fail-safe on any error (escalate to
        an explicit confirm, never a silent auto-approve — R1.5); bounds
        Critic-driven revise cycles per path (R1.7); sets `escalate` for a
        low-confidence PASS (R2.2).
        """
        if self._critic is None or not self._critic_enabled:
            return CriticVerdict(decision=PASS, confidence=1.0, escalate=False)

        target = (step.args or "").strip()
        try:
            current = await asyncio.to_thread(self._read_current_for_critic, target)
            verdict = await self._critic.review(
                goal=self._current_goal or "", path=target,
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
            n = self._critic_revise_counts.get(target, 0) + 1
            self._critic_revise_counts[target] = n
            if n > self._critic_max_revisions:
                log.info("DevAgent: critic revise budget exhausted for %s — "
                         "escalate+allow", target)
                verdict.decision = PASS
                verdict.escalate = True
                return verdict

        # R2.2 — a low-confidence PASS still requires an explicit confirm.
        if verdict.decision == PASS and verdict.confidence < self._critic_confidence_floor:
            verdict.escalate = True
        return verdict

    @staticmethod
    def _read_current_for_critic(path_str: str) -> str:
        """Current on-disk text for the diff the Critic reviews ('' if new file)."""
        from pathlib import Path as _P
        p = _P(path_str.strip().strip("'\""))
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    @staticmethod
    def _critic_reject_message(step: AgentStep, verdict: CriticVerdict) -> str:
        """Diagnostic step result for a blocked/revise edit → drives _replan."""
        target = (step.args or "").strip()
        return (f"{step.action.upper()} to {target[:60]} {verdict.decision} by critic: "
                f"{verdict.summary()}"
                + (f" | suggested fix: {verdict.suggested_fix}" if verdict.suggested_fix else ""))

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
        # model can diagnose; truncate successes to avoid prompt bloat. The
        # trajectory compactor (spec specs/trajectory-reduction/) reproduces this
        # 200/600 budget byte-for-byte when reduction is off, and abstracts older
        # steps when DA_TRAJECTORY_REDUCE is on.
        from inference.trajectory import render_trajectory, reduction_enabled, dedup_enabled
        traj_text, _ = render_trajectory(
            steps, style="reflect", success_chars=200, failure_chars=600,
            readonly_verbs=self._PARALLEL_VERBS, enabled=reduction_enabled(),
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


    # ---------------------------------------------------------------------- #
    # Skill execution (MCP-client tool calls)
    # ---------------------------------------------------------------------- #





    # ---------------------------------------------------------------------- #
    # Math CAS verification
    # ---------------------------------------------------------------------- #





    # ---------------------------------------------------------------------- #
    # Dev action implementations
    # ---------------------------------------------------------------------- #


    # Approval-card diff cap (specs/chat-workbench-parity R5.1).
    _CONFIRM_DIFF_MAX_LINES = 400






    # ── Git safety confirmation ──────────────────────────────────────────────

    # Verbs that mutate state visible to others or that are hard to reverse.
    _GIT_DESTRUCTIVE_VERBS: frozenset[str] = frozenset({
        "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR"
    })




    # ── Git implementations ──────────────────────────────────────────────────








    # ── Context helpers ──────────────────────────────────────────────────────




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
                # The run header must be a direct call because it returns the
                # run_id the steps reference; write_state() returns None. Use the
                # sanctioned _db() seam rather than reaching into _memory._db
                # (private attr). Per-step records go through write_state() below.
                run_id = await self._db().insert_agent_run(
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
        run_id = await self._agent_db.runs.insert_agent_run(
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
            await self._agent_db.runs.insert_agent_step(
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
            "ide_bridge": (
                self._bridge.get_status() if self._bridge is not None else "not wired"
            ),
            "plan": self.get_plan_status(),
        }

    def get_last_result(self) -> Optional[AgentResult]:
        return self._results_log[-1] if self._results_log else None
    def __getattr__(self, name):
        # Prevent recursion if these aren't set yet during __init__
        if name in ('_step_executor', '_saga_manager', '_context_builder'):
            raise AttributeError(name)
        for delegate in ('_step_executor', '_saga_manager', '_context_builder'):
            if delegate in self.__dict__:
                obj = self.__dict__[delegate]
                if name in dir(type(obj)) or name in obj.__dict__:
                    return getattr(obj, name)
        raise AttributeError(f"'DevAgent' object has no attribute '{name}'")

    # Class-level aliases for static/class helpers moved to SagaManager and
    # StepExecutor during the god-object split. The instance __getattr__ above
    # cannot serve class-level access (DevAgent._snapshot_for_write(...)), which
    # tests and external callers rely on — all are static/classmethods with no
    # DevAgent instance state.
    _snapshot_for_write = SagaManager._snapshot_for_write
    _compensation_for = SagaManager._compensation_for
    _restore_file = SagaManager._restore_file
    _saga_git_backend_enabled = SagaManager._saga_git_backend_enabled
    _git_cat_blob = SagaManager._git_cat_blob
    _grep = StepExecutor._grep
    _run_terminal = StepExecutor._run_terminal
    _git_commit = StepExecutor._git_commit
