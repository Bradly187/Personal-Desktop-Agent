"""HybridCoordinator — 4-gate routing between local LLM and Anthropic cloud.

Receives a Command from FusionEngine, decides whether to run local inference
or fall back to the cloud, executes the resulting action, and logs the outcome.

Gate logic (source-dependent):
  touch / multimodal                → bypass all 4 gates → local
  voice_local                       → skip Gate 1 → gates 2-4
  gesture / voice                   → full 4-gate evaluation

Gate 0 — Privacy:     command text contains no sensitive-data patterns
  fail → force local (never send to cloud)
  NOTE: bypass sources (touch / multimodal) are already local-only and never
  reach the cloud, so Gate 0 does not apply to them — a resolved touch action
  executes directly without LLM interference even when its text matches a
  sensitive pattern.

Gate 1 — Confidence:  whisper_logprob ≥ min AND gesture_conf ≥ min
  fail-voice, KNOWN misrecognition (vocab pass already fixed it) → local
  fail-voice, UNKNOWN low-confidence transcript                  → cloud
    (the cloud system prompt is tuned to repair voice misrecognitions the
     local dictionary cannot)
  fail-gesture  → discard silently

Gate 2 — Complexity:  token_count ≤ max AND no complexity keywords
  fail → Anthropic API

Gate 3 — VRAM:  vram_free_gb ≥ vram_free_min_gb  (via pynvml)
  fail → Anthropic API

Gate 4 — Latency EMA:  latency_ema_ms ≤ latency_budget_ms
  fail → Anthropic API

After inference: log outcome to agent.db (AgentDB), call CommandExecutor.execute().
Each log entry includes `gate_that_decided`: the decisive routing factor, one of
_GATE_DECISION_LABELS ("bypass", "gate0_privacy", "gate1_voice_conf",
"gate2_complexity", "gate3_vram", "gate4_latency", "all_pass"). Silently
discarded gestures (Gate 1, low confidence) return early WITHOUT a DB row, so
"discard" is intentionally not a logged label.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from core.command_executor import Command, CommandExecutor
from core.gate_evaluator import GateEvaluator
from core.inference_runner import InferenceRunner, _CloudInference
from core.action_executor import ActionExecutor
from core.workflow_handler import WorkflowHandler
from core.coordinator_state import CoordinatorState
from core.correction_handler import CorrectionHandler
from core.event_dispatcher import EventDispatcher
from core.voice_system_control import VoiceSystemControl
# Back-compat re-export (B1): _BYPASS_SOURCES / _SKIP_GATE1_SOURCES live in
# core.routing_constants, but callers still import them from here (event_dispatcher
# runtime import + tests). Keep the bridge so the single-source split stays internal.
from core.routing_constants import _BYPASS_SOURCES, _SKIP_GATE1_SOURCES  # noqa: F401
from dataclasses import replace as _dc_replace
from inference.local_inference import (
    LocalInference,
    OllamaInference,
)
from desktop.vision_grounder import VisionGrounder
from core.conversation_state import ConversationState
from core.conversation_mode import ConversationMode, conversation_mode_config
from core.slo import SLOConfig
from monitoring.trace import get_tracer

if TYPE_CHECKING:
    from storage.audit_log import AuditLog
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from adaptive.content_filter import ContentFilter
    from adaptive.continuous_trainer import ContinuousTrainer
    from storage.db import AgentDB
    from inference.dev_agent import DevAgent

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CoordinatorConfig:
    # Gate 0 — Privacy (force local when sensitive patterns detected)
    gate0_enabled: bool = True
    gate0_sensitive_patterns: tuple[str, ...] = field(default_factory=lambda: (
        "password", "passwd", "secret", "api key", "api_key", "token",
        "credit card", "card number", "cvv", "ssn", "social security",
        "routing number", "account number", "private key", "ssh key",
    ))

    # Gate 1 thresholds
    whisper_logprob_min: float = -1.0       # log-prob (0 = perfect, -∞ = impossible)
    gesture_confidence_min: float = 0.6

    # Gate 2 thresholds
    max_local_tokens: int = 40              # rough word-count proxy

    # Gate 3 thresholds — 8.0 GB floor suits RTX 5090 (32 GB VRAM); lower for smaller GPUs
    vram_free_min_gb: float = 8.0

    # Gate 4 thresholds
    latency_budget_ms: float = 600.0        # global default / command-domain fallback
    latency_ema_alpha: float = 0.1          # smoothing factor for EMA

    # Per-domain SLOs (gap H). Gate 4 sources its budget via latency_budget_for();
    # the trainer may set per-domain overrides from observed SLO breaches. Command
    # keeps the 600 ms default (preserves prior Gate-4 behaviour exactly).
    slo: SLOConfig = field(default_factory=SLOConfig)
    per_domain_latency_budget: dict = field(default_factory=dict)

    # Local-inference circuit-breaker — a hung local call (Ollama wedged, GPU
    # stuck mid-flare, model reload stall) must not stall the accessibility
    # pipeline indefinitely. Warm wall p50 is ~190ms and an 8B cold load ~2.6s
    # (Ollama 0.30.6, RTX 5090), so 15s is ~6x the worst legitimate case while
    # still catching a true hang.
    local_timeout_s: float = 15.0

    # (routing_log_path removed — outcomes written to agent.db commands table)

    # Anthropic API (cloud fallback). Haiku 4.5 — fast/cheap, 8/8 on voice
    # misrecognitions; the alias floats to the latest 4.5 snapshot. The command
    # path needs only a one-line verb, so Haiku is the right tier here (the dev
    # path uses Opus 4.8 via CloudDevAgent).
    anthropic_model: str = "claude-haiku-4-5"

    def latency_budget_for(self, domain: str = "command") -> float:
        """Gate-4 latency budget for a domain (gap H).

        Resolution order: per-domain override (trainer-set) → per-domain SLO →
        the legacy global `latency_budget_ms`. Command + unknown domains fall back
        to the legacy field, so Gate-4 behaviour is unchanged until an override or
        non-command SLO applies.
        """
        if domain in self.per_domain_latency_budget:
            return self.per_domain_latency_budget[domain]
        if domain != "command":
            return self.slo.latency_budget_ms(domain)
        return self.latency_budget_ms


# ---------------------------------------------------------------------------
# Gate 1 — voice confidence fallback
# ---------------------------------------------------------------------------

# Phonetic vocabulary corrections for the most common voice misrecognitions.
# Keyed on lowercased word/phrase, mapped to the correct desktop-control word.
_VOICE_CORRECTIONS: dict[str, str] = {
    "clothes":     "close",
    "clothe":      "close",
    "scroll done": "scroll down",
    "clique":      "click",
    "tight":       "type",
    "oh pen":      "open",
    "oh pen up":   "open up",
    "hot key":     "hotkey",
    "screen shot": "screenshot",
    # App name phonetic corrections
    "key row":     "vscode",
    "key-row":     "vscode",
    "keyrow":      "vscode",
    "cairo":       "vscode",
    "slap":        "slack",
    "diskord":     "discord",
    "dis cord":    "discord",
}
# RLock so add_personal_corrections (write) and _apply_vocabulary_corrections
# (read-then-iterate) are safe when called concurrently from asyncio.to_thread
# workers on different threads (#3).
_VOICE_CORRECTIONS_LOCK = threading.RLock()


def _apply_vocabulary_corrections(text: str) -> tuple[str, bool]:
    """Replace known voice misrecognitions with the intended desktop-control word.

    Returns (corrected_text, changed).  Checks multi-word phrases first so
    'scroll done' beats 'done' matching nothing.
    """
    lower = text.lower()
    # Snapshot the corrections dict under the lock so a concurrent write from
    # add_personal_corrections doesn't mutate the dict mid-iteration (#3).
    with _VOICE_CORRECTIONS_LOCK:
        corrections_snapshot = sorted(
            _VOICE_CORRECTIONS.items(), key=lambda kv: -len(kv[0])
        )
    for wrong, right in corrections_snapshot:
        if wrong in lower:
            corrected = lower.replace(wrong, right)
            # Restore capitalisation: capitalize the first token; preserve
            # all-caps acronyms (VS, AI, etc.) from the original where they
            # weren't part of the corrected span (#17).
            orig_tokens = text.split()
            corr_tokens = corrected.split()
            result_tokens = []
            for i, tok in enumerate(corr_tokens):
                orig_tok = orig_tokens[i] if i < len(orig_tokens) else ""
                if i == 0:
                    result_tokens.append(tok.capitalize())
                elif orig_tok.isupper() and len(orig_tok) > 1:
                    result_tokens.append(tok.upper())
                else:
                    result_tokens.append(tok)
            return " ".join(result_tokens), True
    return text, False


async def _retranscribe(cmd: Command) -> Command:
    """Apply vocabulary correction to a low-confidence voice transcript before Gate 2."""
    corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
    if changed:
        log.info("Gate 1 vocab correction: %r → %r", cmd.text, corrected_text)
        cmd = _dc_replace(cmd, text=corrected_text, whisper_logprob=0.0)
    return cmd




# ---------------------------------------------------------------------------
# HybridCoordinator
# ---------------------------------------------------------------------------


# Output schema for the command-path LLM. The local/cloud command models are
# prompted to answer verb-first with exactly one of these 11 accessibility
# verbs (dev verbs are handled by DevAgent before the gate path; SNAP_WINDOW is
# gesture-sourced and never an LLM output). Any response whose first token is
# not one of these is a malformed LLM output and is degraded to CLARIFY in
# _execute_action() rather than dispatched as a bad/unknown verb.
_VALID_COMMAND_VERBS: frozenset[str] = frozenset({
    "CLICK", "MOUSEDOWN", "MOUSEUP", "SCROLL", "TYPE", "OPEN",
    "CLOSE", "HOTKEY", "DICTATE", "CLARIFY", "SCREENSHOT",
})

# Exhaustive set of `gate_that_decided` values written to agent.db by route().
# Each names the decisive routing factor for one logged command. Kept as a
# single source of truth so analytics/tests can assert against it. NOTE:
# silently discarded gestures (Gate 1, low confidence) return early WITHOUT a DB
# row, so "discard" is deliberately absent.
_GATE_DECISION_LABELS: frozenset[str] = frozenset({
    "bypass",            # touch / multimodal — local, gates skipped
    "gate0_privacy",     # sensitive text — forced local
    "gate1_voice_conf",  # unknown low-confidence voice — escalated to cloud
    "gate2_complexity",  # complex command — cloud
    "gate3_vram",        # insufficient free VRAM — cloud
    "gate4_latency",     # local latency over budget — cloud
    "all_pass",          # all gates passed — local
})


def _apply_pain_day_adjustments(cfg, snapshot) -> "CoordinatorConfig":
    """Return a modified config copy with pain-day threshold relaxations.
    Does not mutate the original config.
    """
    from dataclasses import replace
    return replace(
        cfg,
        whisper_logprob_min=cfg.whisper_logprob_min - 0.15,
        gesture_confidence_min=cfg.gesture_confidence_min - 0.10,
    )


# Voice phrases handled by the system-control block in route() (pain day,
# lecture mode, condition switching, calibration). The dev-agent pre-gate must
# NOT intercept these: the DomainClassifier maps some (e.g. "pain day on" →
# general) to dev domains, which would shadow the keyword handler and send the
# phrase to an LLM instead. KEEP IN SYNC with the keyword block in route().
class HybridCoordinator:
    # Class-level DomainClassifier — stateless keyword scorer, no need to
    # re-instantiate per route() call.
    _domain_classifier = None

    @classmethod
    def _get_domain_classifier(cls):
        if cls._domain_classifier is None:
            from core.domain_classifier import DomainClassifier
            cls._domain_classifier = DomainClassifier()
        return cls._domain_classifier

    def __init__(
        self,
        local: LocalInference | None = None,
        config: CoordinatorConfig | None = None,
        trainer: Optional["ContinuousTrainer"] = None,
        dev_agent: Optional["DevAgent"] = None,
        agent_db: Optional["AgentDB"] = None,
        session_id: int = -1,
        content_filter: Optional["ContentFilter"] = None,
        audit_log: Optional["AuditLog"] = None,
        twin_state: Optional["BehavioralTwinState"] = None,
        whisper_stream=None,
    ) -> None:
        self._local = local or OllamaInference()
        self._cfg = config or CoordinatorConfig()
        self._cloud = _CloudInference(self._cfg.anthropic_model)
        self._executor = CommandExecutor()
        self._trainer = trainer
        self._dev_agent = dev_agent
        self._skill_registry = None     # SkillRegistry | None — voice 'help' listing
        self._personal_kb = None        # PersonalKB | None — voice 'index my notes'
        self._macro_store = None        # MacroStore | None — self-skilling rung 2
        self._agent_db = agent_db
        self._session_id = session_id
        self._content_filter = content_filter
        self._audit = audit_log
        self._twin = twin_state
        self._inference = InferenceRunner(
            self._cfg,
            # Late-bound: several of these (rate_limiter especially) are wired
            # onto the coordinator *after* construction via a set_* method, and
            # tests routinely monkeypatch coord._content_filter / coord._cloud /
            # etc. post-construction — a captured-by-value reference would
            # silently go stale.
            local=lambda: self._local,
            cloud=lambda: self._cloud,
            trainer=lambda: self._trainer,
            agent_db=lambda: self._agent_db,
            content_filter=lambda: self._content_filter,
            rate_limiter=lambda: self._rate_limiter,
            note_cloud_call=lambda: self._gates.note_cloud_call(),
        )
        self._gates = GateEvaluator(
            self._cfg,
            # Late-bound for the same reason as InferenceRunner above.
            run_local=lambda cmd: self._inference.run_local(cmd),
            run_cloud=lambda cmd: self._inference.run_cloud(cmd),
            approval_config=lambda: self._approval_config(),
            audit=self._audit,
            tts_speak=lambda text: self._tts_speak(text),
        )
        self._grounder = VisionGrounder()
        self._whisper = whisper_stream
        self._fusion = None   # set via set_fusion_engine() after FusionEngine is created
        self.state = CoordinatorState()
        self._conversation = ConversationState()  # voice anaphora + last-action hint
        self._action_executor = ActionExecutor(
            # Late-bound for the same reason as InferenceRunner/GateEvaluator
            # above — several of these (metrics, whisper, bridge, target_cache)
            # are wired onto the coordinator after construction via a set_*
            # method.
            executor=lambda: self._executor,
            grounder=lambda: self._grounder,
            conversation=lambda: self._conversation,
            metrics=lambda: self._metrics,
            whisper=lambda: self._whisper,
            bridge=lambda: self._bridge,
            target_cache=lambda: self._target_cache,
            state=self.state,
        )
        # Multi-agent workflow voice trigger ("think hard about …"). Default OFF;
        # the runner is injected by main.py and gates itself on
        # workflow_orchestration.enabled. Spec: specs/workflow-orchestration/.

        # Voice conversation mode (wake/sleep-gated talk-only dialogue). Default
        # OFF; reads conversation_mode.enabled from ~/.claude/ipad_bridge/config.json.
        # Spec: specs/conversation-mode/.
        self._conv_mode = ConversationMode.from_config(conversation_mode_config())
        self._workflow = WorkflowHandler(
            # Late-bound for the same reason as the other extracted modules —
            # workflow_runner/dev_agent/macro_store are wired onto the
            # coordinator after construction via a set_* method, and tests
            # routinely set attributes like coord._wf_cfg / coord._twin
            # directly.
            dev_agent=lambda: self._dev_agent,
            twin=lambda: self._twin,
            conv_mode=lambda: self._conv_mode,
            macro_store=lambda: self._macro_store,
            agent_db=lambda: self._agent_db,
            executor=lambda: self._executor,
            tts_speak=lambda text: self._tts_speak(text),
            speak_and_suppress=lambda text: self._speak_and_suppress(text),
        )
        self._profiler = None    # set via set_profiler()
        self._calibrator = None  # set via set_calibrator()
        self._voice_control = VoiceSystemControl(
            # Explicit DI (specs/hybrid-coordinator-decomposition R3): every
            # dependency is a named accessor, no back-reference to self. Late-
            # bound for the same reason as the other extracted modules.
            whisper=lambda: self._whisper,
            agent_db=lambda: self._agent_db,
            twin=lambda: self._twin,
            profiler=lambda: self._profiler,
            calibrator=lambda: self._calibrator,
            dev_agent=lambda: self._dev_agent,
            personal_kb=lambda: self._personal_kb,
            skill_registry=lambda: self._skill_registry,
            audit=lambda: self._audit,
            tts_speak=lambda text: self._tts_speak(text),
            approval_config=lambda: self._approval_config(),
            # condition switching / calibration must run on the real
            # coordinator: ConditionProfiler.apply_to() takes coordinator= as
            # an external API kwarg, so these stay coordinator methods and
            # are passed through as narrow delegates rather than moved.
            switch_condition=lambda condition: self._switch_condition(condition),
            run_calibration=lambda condition, quick: self._run_calibration(condition, quick),
        )
        self._correction_handler = CorrectionHandler(
            state=self.state,
            agent_db=lambda: self._agent_db,
            trainer=lambda: self._trainer,
            audit=lambda: self._audit,
            tts_speak=lambda text: self._tts_speak(text),
            session_id=self._session_id,
        )
        self._event_dispatcher = EventDispatcher(self)
        self._metrics = None   # set via set_metrics()

        self._memory = None   # MemoryManager — wired via set_memory()
        self._event_bus = None  # EventBus — wired via set_event_bus()
        self._rate_limiter = None  # RateLimiter — wired via set_rate_limiter()
        self._bridge = None    # IPadBridge — wired via set_bridge() for trace correlation

        # Cloud DevAgent (--cloud-dev-agent) — optional Anthropic-API fallback for
        # dev-domain queries so a 30B specialist (and a GPU wake) is not needed.
        self._cloud_dev_agent = None          # CloudDevAgent or None
        self._cloud_always = False            # True with --no-local-specialists
        self._local_specialist_available = None  # () -> bool (specialist awake?)


    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _on_task_done(task: "asyncio.Task[None]", label: str) -> None:
        """Log any exception from a fire-and-forget task so failures are visible."""
        if not task.cancelled() and task.exception():
            log.error("%s raised: %s", label, task.exception())

    # ---------------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------------- #

    def set_dev_agent(self, dev_agent: "DevAgent") -> None:
        self._dev_agent = dev_agent

    # ── Chat active-directory switching (specs/chat-context-attachments R1) ──
    def list_writable_roots(self) -> dict:
        """Current allowlist + active root for the chat picker (R1.5)."""
        return {"active_root": self._executor.active_root,
                "writable_roots": self._executor.writable_roots}

    def set_active_directory(self, path: str, *, confirm: bool = False) -> dict:
        """Activate ``path`` as the chat session's working dir (browse + confirm,
        R1.2–R1.4 / AGENTS.md #7).

        Returns ``{status, path, ...}`` where status is:
          - ``"invalid"``          — not a real directory; nothing changed.
          - ``"confirm_required"`` — a new root (outside the allowlist) and
            ``confirm`` was False; nothing changed, the UI must re-send with
            ``confirm=True``.
          - ``"activated"``        — the root is now active (appended to the
            allowlist if it was new) and the DevAgent re-points to it.
        """
        import os as _os
        from core.goal_session import _path_in_scope
        rp = _os.path.realpath(_os.path.expanduser(path or ""))
        if not _os.path.isdir(rp):
            return {"status": "invalid", "path": path}
        already = _path_in_scope(rp, self._executor.writable_roots)
        if not already and not confirm:
            return {"status": "confirm_required", "path": rp}
        # Already in scope, or an explicit confirm → activate (appends if new).
        self._executor.set_active_root(rp)
        if self._dev_agent is not None:
            self._dev_agent.set_repo_root(rp)
        log.info("HybridCoordinator: active directory → %s (confirm=%s)", rp, confirm)
        return {"status": "activated", "path": rp,
                "active_root": self._executor.active_root,
                "writable_roots": self._executor.writable_roots}

    # ── Chat run controls (specs/chat-workbench-parity R4/R8.3) ─────────────
    def request_dev_cancel(self) -> bool:
        """Gracefully cancel the in-flight DevAgent plan (stops after the
        current step; the saga owns any rollback). Returns whether a DevAgent
        is wired — the chat Stop button uses this alongside cancelling its own
        await, mirroring the voice "cancel task" path."""
        if self._dev_agent is None:
            return False
        self._dev_agent.request_cancel()
        return True

    async def revert_last_dev_run(self, trace_id: str = "") -> bool:
        """Chat "Undo this run": delegate to the DevAgent's VoiceRewindHandler
        path. The rollback stays gated on the existing fail-safe-DENY confirm
        (surfaced as a chat approval card when trace_id is set)."""
        if self._dev_agent is None:
            return False
        return await self._dev_agent.revert_last_run(trace_id=trace_id)


    def set_skill_registry(self, registry) -> None:
        """Wire the SkillRegistry so the voice 'help' command can list skills."""
        self._skill_registry = registry

    def set_personal_kb(self, kb) -> None:
        """Wire the PersonalKB for the voice 'index my notes' command."""
        self._personal_kb = kb

    def set_macro_store(self, store) -> None:
        """Wire the MacroStore for self-skilling rung 2 (macro routing/replay)."""
        self._macro_store = store

    async def note_pending_macro(self, summaries: list) -> None:
        """Detector callback: a macro was detected — announce it and arm the
        "save that as ..." approval. Public API — wired by main.py as the
        macro detector's on_staged callback."""
        await self._workflow.note_pending_macro(summaries)

    def set_cloud_dev_agent(
        self,
        cloud_agent,
        *,
        always_cloud: bool = False,
        local_available_fn=None,
    ) -> None:
        """Wire a CloudDevAgent for dev-domain routing.

        always_cloud=True  → every dev-domain query goes to the cloud (used with
                             --no-local-specialists; no GPU specialist is woken).
        always_cloud=False → cloud is a fallback: a dev query goes local when a
                             local specialist is already awake, else to the cloud
                             (avoids a ~50 s GPU wake when the pool is idle/torn
                             down, freeing the GPU for the command path).
        local_available_fn → callable () -> bool, True when a local specialist is
                             currently GPU-resident.
        """
        self._cloud_dev_agent = cloud_agent
        self._cloud_always = always_cloud
        self._local_specialist_available = local_available_fn
        log.info(
            "HybridCoordinator: CloudDevAgent wired (always_cloud=%s, model=%s)",
            always_cloud, getattr(cloud_agent, "model", "?"),
        )

    def _should_route_cloud_dev(self) -> bool:
        """Decide whether a dev-domain query should go to the cloud agent."""
        if self._cloud_dev_agent is None:
            return False
        if self._cloud_always:
            return True
        # Fallback mode: cloud only when no local specialist is currently awake.
        if self._local_specialist_available is None:
            return False
        try:
            return not self._local_specialist_available()
        except Exception as exc:
            log.debug("local-specialist availability check failed: %s", exc)
            return False

    def _record_dev_command(self, text: str) -> None:
        self.state.recent_dev_commands.append(text)
        if len(self.state.recent_dev_commands) > 10:
            self.state.recent_dev_commands = self.state.recent_dev_commands[-10:]

    def set_whisper_stream(self, whisper_stream) -> None:
        self._whisper = whisper_stream

    def set_profiler(self, profiler) -> None:
        self._profiler = profiler

    def set_calibrator(self, calibrator) -> None:
        self._calibrator = calibrator

    def set_metrics(self, metrics) -> None:
        """Wire the global Metrics singleton for real-time observability."""
        self._metrics = metrics

    def add_personal_corrections(self, corrections: dict) -> None:
        """Merge condition-specific corrections into the live _VOICE_CORRECTIONS map."""
        global _VOICE_CORRECTIONS
        with _VOICE_CORRECTIONS_LOCK:  # guard concurrent reads in _apply_vocabulary_corrections (#3)
            for heard, expected in corrections.items():
                if heard not in _VOICE_CORRECTIONS:
                    _VOICE_CORRECTIONS[heard] = expected
                    log.info("Personal correction loaded: %r → %r", heard, expected)

    async def _switch_condition(self, condition: str) -> None:
        """Load and apply a voice profile for the given condition."""
        if self._profiler and hasattr(self._profiler, 'load'):
            await self._profiler.load(condition)
            if self._whisper:
                await self._profiler.apply_to(self._whisper, coordinator=self)
            log.info("Condition switched to: %s", condition)

    async def _run_calibration(self, condition: str, quick: bool) -> None:
        """Start a guided voice calibration session."""
        if self._calibrator:
            report = await self._calibrator.run(condition=condition, quick=quick)
            # Apply the newly built profile immediately
            await self._switch_condition(condition)
            log.info(
                "Calibration complete: condition=%s accuracy=%.0f%% corrections=%d",
                report.condition, report.accuracy * 100, report.corrections_added,
            )

    def set_fusion_engine(self, fusion_engine) -> None:
        """Wire FusionEngine so pain-day thresholds propagate on each route()."""
        self._fusion = fusion_engine

    def set_memory(self, memory) -> None:
        """Wire MemoryManager for standardised storage access."""
        self._memory = memory

    def set_event_bus(self, bus) -> None:
        """Wire EventBus so gate decisions and command outcomes are published as events."""
        self._event_bus = bus

    def set_rate_limiter(self, limiter) -> None:
        """Wire the RateLimiter so cloud (Anthropic) calls are throttled. The
        CloudDevAgent shares the same limiter via set_cloud_dev_agent so both
        cloud egress paths count against one 'anthropic' bucket."""
        self._rate_limiter = limiter
        if self._cloud_dev_agent is not None and hasattr(
            self._cloud_dev_agent, "set_rate_limiter"
        ):
            self._cloud_dev_agent.set_rate_limiter(limiter)

    def set_bridge(self, bridge) -> None:
        """Wire IPadBridge for trace_id correlation on iPad log entries."""
        self._bridge = bridge

    def set_target_cache(self, cache) -> None:
        """Wire the ClickableTargetCache so click-target CLARIFYs can render a
        palette of on-screen elements (Phase 3 prototype)."""
        self._target_cache = cache

    def _approval_config(self) -> dict:
        """Load approval_config.json; returns {} on failure (safe defaults used by callers).

        Result is cached for 60 s so repeated calls in the hot path (authorize,
        cloud-budget check) don't pay a disk read on every cloud call (#6).
        """
        now = time.monotonic()
        cache = getattr(self, "_approval_cfg_cache", None)
        if cache is not None:
            cached_at, cached_val = cache
            if now - cached_at < 60.0:
                return cached_val
        try:
            import json as _json
            from pathlib import Path
            p = Path(__file__).parent.parent / "approval_config.json"
            result = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            result = {}
        self._approval_cfg_cache: tuple[float, dict] = (now, result)
        return result

    async def _tts_speak(self, text: str) -> None:
        """Speak text via the configured TTS backend (fire-and-forget safe)."""
        try:
            from tts.polly_stream import get_client as _get_tts
            await asyncio.to_thread(_get_tts().speak_sync, text)
        except Exception as exc:
            log.debug("HybridCoordinator._tts_speak failed: %s", exc)

    async def _speak_and_suppress(self, text: str) -> None:
        """Speak ``text`` while suppressing the mic for the playback duration.

        Without this guard the agent transcribes its own TTS voice as the user's
        next utterance — a runaway feedback loop. We suppress generously *before*
        speaking (also flushing any buffered audio), then re-arm a short echo
        tail *after* ``_tts_speak`` returns (it blocks until playback finishes),
        so the window always covers synthesis + playback even if the word-count
        estimate runs short. Mirrors the voice-calibrator / approval-gate guard.
        """
        words = len(text.split())
        # ~0.35 s/word is the spoken-rate estimate used across the voice paths.
        est = max(1.5, words * 0.35)
        whisper = self._whisper
        if whisper is not None:
            try:
                whisper.suppress(est + 1.0)
            except Exception as exc:
                log.debug("tts: pre-speak suppress failed: %s", exc)
        await self._tts_speak(text)
        if whisper is not None:
            try:
                whisper.suppress(0.8)   # echo tail after playback completes
            except Exception as exc:
                log.debug("tts: post-speak suppress failed: %s", exc)

    async def route(self, cmd: Command) -> dict:
        """Route a Command, then durably persist its trace for eval replay (GAP-4).

        Thin wrapper over `_route_impl`. When tracing is on, the completed trace's
        spans are flushed to AgentDB fire-and-forget AFTER the command finishes —
        never on the hot path, and a persistence fault never affects the result.
        """
        try:
            return await self._route_impl(cmd)
        finally:
            try:
                _tr = get_tracer()
                if _tr.enabled and cmd.trace_id and self._agent_db and self._agent_db.available:
                    from core.async_utils import fire_and_log
                    fire_and_log(
                        _tr.persist_trace(cmd.trace_id, self._agent_db, self._session_id),
                        log, label="persist trace",
                    )
            except Exception:  # pragma: no cover - persistence must never break route
                pass

    async def _route_impl(self, cmd: Command) -> dict:
        return await self._event_dispatcher.route_impl(cmd)

    # --- Legacy state aliases (pre-CoordinatorState API) --------------------
    # Tests and older callers read/write these directly on the coordinator —
    # including on __new__-constructed instances — so proxy them to self.state,
    # creating it lazily when __init__ never ran.
    def _ensure_state(self) -> CoordinatorState:
        st = self.__dict__.get("state")
        if st is None:
            st = CoordinatorState()
            self.__dict__["state"] = st
        return st

    @property
    def _pending_clarification(self):
        return self._ensure_state().pending_clarification

    @_pending_clarification.setter
    def _pending_clarification(self, v):
        self._ensure_state().pending_clarification = v

    @property
    def _active_clarify_surface_id(self):
        return self._ensure_state().active_clarify_surface_id

    @_active_clarify_surface_id.setter
    def _active_clarify_surface_id(self, v):
        self._ensure_state().active_clarify_surface_id = v

    @property
    def _recent_open_targets(self):
        return self._ensure_state().recent_open_targets

    @_recent_open_targets.setter
    def _recent_open_targets(self, v):
        self._ensure_state().recent_open_targets = v

    @property
    def _recent_dev_commands(self):
        return self._ensure_state().recent_dev_commands

    @_recent_dev_commands.setter
    def _recent_dev_commands(self, v):
        self._ensure_state().recent_dev_commands = v

    @property
    def _last_executed_action(self):
        return self._ensure_state().last_executed_action

    @_last_executed_action.setter
    def _last_executed_action(self, v):
        self._ensure_state().last_executed_action = v

    @property
    def _last_command_id(self):
        return self._ensure_state().last_command_id

    @_last_command_id.setter
    def _last_command_id(self, v):
        self._ensure_state().last_command_id = v

    def _get_correction_handler(self) -> CorrectionHandler:
        """Return the wired CorrectionHandler, building one lazily for
        partially-constructed coordinators (tests use __new__ + a couple of
        attributes to exercise the correction path in isolation)."""
        h = self.__dict__.get("_correction_handler")
        if h is None:
            h = CorrectionHandler(
                state=self._ensure_state(),
                agent_db=lambda: self.__dict__.get("_agent_db"),
                trainer=lambda: self.__dict__.get("_trainer"),
                audit=lambda: self.__dict__.get("_audit"),
                tts_speak=lambda text: (
                    self._tts_speak(text)
                    if self.__dict__.get("_tts_speak") else None
                ),
                session_id=self.__dict__.get("_session_id", -1),
            )
            self.__dict__["_correction_handler"] = h
        return h

    def _harvest_correction(self, cmd: Command, prior_action: str) -> None:
        self._get_correction_handler().harvest_correction(cmd, prior_action)

    async def correct(self, cmd: Command, wrong_action: str, correct_action: str) -> dict:
        return await self._get_correction_handler().correct(cmd, wrong_action, correct_action)

    async def _on_correction(self, cmd: Command, correct_action: str) -> None:
        await self._get_correction_handler().on_correction(cmd, correct_action)

    def get_status(self) -> dict:
        return {
            "local_backend": self._local.get_status(),
            "latency_ema_ms": round(self._gates.latency_ema, 1) if self._gates.latency_ema else None,
            "config": {
                "whisper_logprob_min": self._cfg.whisper_logprob_min,
                "gesture_confidence_min": self._cfg.gesture_confidence_min,
                "max_local_tokens": self._cfg.max_local_tokens,
                "vram_free_min_gb": self._cfg.vram_free_min_gb,
                "latency_budget_ms": self._cfg.latency_budget_ms,
            },
        }
