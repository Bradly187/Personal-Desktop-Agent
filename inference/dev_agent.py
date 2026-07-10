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
# Back-compat re-exports: these moved to plan_parser in the god-object split,
# but external callers (macro_store, macro_detector, tests) still import them
# from here. Keep the bridge so the split stays internal.
from inference.plan_parser import _PLAN_ACTIONS, _parse_deps, _extract_json_obj, _STEP_PATTERN  # noqa: F401
from inference.context_builder import ContextBuilder
from inference.saga_manager import SagaManager




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
        # extra_ctx ahead of the dynamic RAG/git-status context. Default ON
        # (DA_REPO_CONTEXT). Set DA_REPO_CONTEXT=0 to disable.
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
        # Default ON (DA_DELEGATE). Set DA_DELEGATE=0 to disable; a stray DELEGATE
        # step with the flag OFF is a safe no-op.
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
            from inference.executors.skill_executor import handle_skill
            return await handle_skill(self, text)

        # Personal-document questions ("what did I write in my notes about …")
        # answer from the PersonalKB — never from model hallucination.
        if self._personal_kb is not None and getattr(self._personal_kb, "available", False) \
                and _is_personal_query(text):
            from inference.executors.skill_executor import handle_personal_query
            return await handle_personal_query(self, text)

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
            from inference.executors.math_verifier import verify_math_with_cas
            note = await verify_math_with_cas(self, text, router_result.text)
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





    # ── Parallel context-gathering (gap #1) ─────────────────────────────────



    # ── Dependency-DAG wave execution (gap A) ────────────────────────────────






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























    # ---------------------------------------------------------------------- #
    # Planner-driven DELEGATE — bounded read-only sub-agent (Gap D)
    # ---------------------------------------------------------------------- #






    # ---------------------------------------------------------------------- #
    # Plan-level authorization helpers
    # ---------------------------------------------------------------------- #


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



    # Restored class-level aliases
    _compensation_for = __import__('inference.saga_manager', fromlist=['SagaManager']).SagaManager._compensation_for
    _snapshot_for_write = __import__('inference.saga_manager', fromlist=['SagaManager']).SagaManager._snapshot_for_write
    _restore_file = __import__('inference.saga_manager', fromlist=['SagaManager']).SagaManager._restore_file

    # ---------------------------------------------------------------------- #
    # Pure Function Delegates
    # ---------------------------------------------------------------------- #

    async def _execute_step(self, step):
        from inference.step_executor import execute_step
        return await execute_step(self, step)

    async def _confirm_destructive_op(self, description: str, *, force: bool = False, card=None) -> bool:
        from inference.step_executor import confirm_destructive_op
        return await confirm_destructive_op(self, description, force=force, card=card)

    def _apply_edit(self, path_str: str, body: str, edit_format: str | None = None) -> str:
        from inference.step_executor import apply_edit
        if edit_format is None:
            edit_format = self._router.edit_format_for(self._active_plan_model)
        return apply_edit(path_str, body, edit_format, self._edit_applier)

    def _write_file(self, path_str: str, content: str) -> str:
        from inference.step_executor import write_file
        return write_file(path_str, content)

    def _read_file(self, path_str: str, max_chars: int = 8000) -> str:
        from inference.step_executor import read_file
        return read_file(path_str, max_chars)

    def _diff_for_confirm(self, path_str: str, new_text: str) -> str:
        from inference.step_executor import diff_for_confirm
        return diff_for_confirm(path_str, new_text)

    @staticmethod
    def _grep(pattern: str, search_path: str, max_lines: int = 100) -> str:
        from inference.step_executor import grep
        return grep(pattern, search_path, max_lines)
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
        if name in ('_saga_manager', '_context_builder'):
            raise AttributeError(name)
            
        # 1. Existing object delegates
        for delegate in ('_saga_manager', '_context_builder'):
            if delegate in self.__dict__:
                obj = self.__dict__[delegate]
                if name in dir(type(obj)) or name in obj.__dict__:
                    return getattr(obj, name)
                    
        # 2. Forward to pure function executors
        import inference.executors.plan_executor as pe
        import inference.executors.subagent_delegator as sd
        import inference.executors.evaluation_manager as em
        
        for mod in (pe, sd, em):
            if hasattr(mod, name):
                func = getattr(mod, name)
                # Ensure it's a function defined in that module to prevent leaking imports
                if callable(func) and getattr(func, "__module__", "") == mod.__name__:
                    from functools import partial
                    return partial(func, self)
                    
        raise AttributeError(f"'DevAgent' object has no attribute '{name}'")

    # Class-level aliases for static/class helpers moved to SagaManager and
    
    @staticmethod
    def _run_terminal(cmd: str) -> str:
        from inference.executors.terminal_executor import run_terminal
        return run_terminal(cmd)
        
    @staticmethod
    def _git_commit(message: str) -> str:
        from inference.executors.git_executor import git_commit
        return git_commit(message)

    @staticmethod
    async def _fetch_url(url: str) -> str:
        from inference.executors.web_executor import fetch_url
        return await fetch_url(url)
        
    async def _execute_skill_step(self, step) -> str:
        from inference.executors.skill_executor import execute_skill_step
        return await execute_skill_step(self, step)
        
    async def _handle_skill(self, text: str):
        from inference.executors.skill_executor import handle_skill
        return await handle_skill(self, text)
        
    @staticmethod
    async def _scan_web_content(url: str, text: str) -> str:
        from inference.executors.web_executor import scan_web_content
        return await scan_web_content(url, text)
        
    @staticmethod
    async def _capture_screenshot() -> str:
        from inference.executors.web_executor import capture_screenshot
        return await capture_screenshot()

