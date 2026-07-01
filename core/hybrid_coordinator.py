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
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.command_executor import Command, CommandExecutor
from core.gate_evaluator import GateEvaluator
from core.inference_runner import InferenceRunner, _CloudInference, _PENDING_INFERENCE_IDS
from core.action_executor import ActionExecutor
from core.workflow_handler import WorkflowHandler
from dataclasses import replace as _dc_replace
from inference.local_inference import (
    LocalInference,
    OllamaInference,
    _build_prompt,
    get_inference_capture,
    set_inference_capture,
)
from desktop.vision_grounder import VisionGrounder
from core.conversation_state import ConversationState
from core.workflow_voice import workflow_voice_config
from core.conversation_mode import ConversationMode, conversation_mode_config
from core.slo import SLOConfig
from monitoring.trace import get_tracer
from monitoring.cost_ledger import estimate_cost

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


def _apply_vocabulary_corrections(text: str) -> tuple[str, bool]:
    """Replace known voice misrecognitions with the intended desktop-control word.

    Returns (corrected_text, changed).  Checks multi-word phrases first so
    'scroll done' beats 'done' matching nothing.
    """
    lower = text.lower()
    # Longest-phrase-first so "scroll done" beats a single-word match
    for wrong, right in sorted(_VOICE_CORRECTIONS.items(), key=lambda kv: -len(kv[0])):
        if wrong in lower:
            corrected = lower.replace(wrong, right)
            # Restore original capitalisation pattern (title-case first word)
            parts = corrected.split()
            if parts:
                parts[0] = parts[0].capitalize()
            return " ".join(parts), True
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

_BYPASS_SOURCES = {"touch", "multimodal"}
_SKIP_GATE1_SOURCES = {"voice_local"}

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
_SYSTEM_CONTROL_PHRASES: frozenset[str] = frozenset({
    "start lecture mode", "lecture mode on", "begin lecture mode",
    "stop lecture mode", "lecture mode off", "end lecture mode",
    "pain day on", "flare day on", "bad day",
    "pain day off", "flare day off", "feeling better",
    "this is a good day", "good day mode", "feeling well",
    "this is a flare day", "flare day", "flare mode",
    "this is an allergy day", "allergy day", "allergy mode",
    "run voice calibration", "calibrate my voice", "quick calibration",
    "calibrate flare day", "calibrate allergy day",
    # Goal-level agent control
    "hey agent status", "what are you doing", "agent status",
    "status", "what's happening",
    "hey agent stop", "cancel task", "cancel agent", "stop agent",
    "stop the agent", "cancel the task",
    # Crash recovery — advertised by the post-crash TTS notice in main.py;
    # the resume itself is voice-confirm-gated inside resume_pending_plan()
    "resume task", "resume the task", "hey agent resume",
    "resume work", "resume interrupted task",
    "hey agent history", "what did you do", "agent history",
    "show history", "recent actions",
    "review queue", "show review queue", "what needs review",
    "hey agent review queue", "show escalations", "pending reviews",
    "clear review queue", "dismiss reviews", "clear escalations",
    # Mic mute — voice can only mute; unmute requires the iPad button
    # (mic is deaf once muted, so a voice unmute command can never arrive)
    "mute mic", "mute microphone", "mic off", "silence mic",
    # Capability discovery + personal KB maintenance
    "help", "what can you do", "what can i say", "list your skills",
    "index my notes", "reindex my notes", "index my documents",
    # Google PIM auth lifecycle (one-time setup + expired-token recovery)
    "connect google", "reconnect google", "connect gmail", "set up gmail",
    "set up google",
})

from core.schedule_parser import is_schedule_phrase, parse as parse_schedule
from storage.personal_kb import is_personal_query as _is_personal_query


def _is_system_control_voice(cmd) -> bool:
    """True if `cmd` is a voice system-control keyword that route()'s keyword
    block handles — so the dev-agent pre-gate leaves it alone."""
    if cmd.source not in ("voice", "voice_local"):
        return False
    norm = cmd.text.lower().strip(" \t\n.,!?;:\"'")
    if norm in _SYSTEM_CONTROL_PHRASES:
        return True
    # lecture-notes search is a startswith/contains pattern, not exact-match
    if "lecture notes" in norm and "search" in norm:
        return True
    # "hey agent authorize <goal>" — prefix match
    if norm.startswith("hey agent authorize ") or norm.startswith("authorize "):
        return True
    if is_schedule_phrase(norm):       # N+2: reminders / schedules / event rules
        return True
    return False


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
        self._pending_clarification: Optional[str] = None
        # A2UI: surface_id of a live CLARIFY touch-card, cleared when the
        # clarification resolves (by tap or voice) so a stale card doesn't linger.
        self._active_clarify_surface_id: Optional[str] = None
        # Rolling list of recently-opened app/file targets (most-recent first),
        # used to populate the "what would you like to open?" CLARIFY card.
        self._recent_open_targets: list[str] = []
        # Live clickable-element snapshot source for the click-target palette
        # (Phase 3 prototype, flag-gated by DA_A2UI_CLICK_TARGETS).
        self._target_cache = None
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
            get_pending_clarification=lambda: self._pending_clarification,
            set_pending_clarification=lambda v: setattr(self, "_pending_clarification", v),
            get_active_clarify_surface_id=lambda: self._active_clarify_surface_id,
            set_active_clarify_surface_id=lambda v: setattr(self, "_active_clarify_surface_id", v),
            get_recent_open_targets=lambda: self._recent_open_targets,
            set_recent_open_targets=lambda v: setattr(self, "_recent_open_targets", v),
        )
        # Multi-agent workflow voice trigger ("think hard about …"). Default OFF;
        # the runner is injected by main.py and gates itself on
        # workflow_orchestration.enabled. Spec: specs/workflow-orchestration/.
        self._workflow_runner = None
        self._wf_cfg = workflow_voice_config()
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
            workflow_runner=lambda: self._workflow_runner,
            dev_agent=lambda: self._dev_agent,
            wf_cfg=lambda: self._wf_cfg,
            twin=lambda: self._twin,
            conv_mode=lambda: self._conv_mode,
            macro_store=lambda: self._macro_store,
            agent_db=lambda: self._agent_db,
            executor=lambda: self._executor,
            tts_speak=lambda text: self._tts_speak(text),
            speak_and_suppress=lambda text: self._speak_and_suppress(text),
        )
        self._lecture_mode: bool = False
        self._profiler = None    # set via set_profiler()
        self._calibrator = None  # set via set_calibrator()
        # D8: correction tracking
        self._last_executed_action: str = ""
        self._last_command_id: int = -1

        # GAP-6: intent drift / trust-decay tripwire. The first substantive
        # dev-domain command of a session anchors the intent; subsequent dev
        # commands are compared against it, and a sustained divergence logs a
        # one-time advisory DRIFT_WARNING (the command still runs).
        self._session_intent: Optional[str] = None
        self._drift_streak: int = 0
        self._drift_warned: bool = False

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
        # Small rolling buffer of recent dev queries — passed to the cloud agent
        # as context without a DB round-trip in the hot path.
        self._recent_dev_commands: list[str] = []


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

    def set_workflow_runner(self, runner) -> None:
        """Wire the multi-agent WorkflowRunner so the voice 'think hard about …'
        trigger can fan a goal out to fresh-context sub-agents. The runner gates
        itself on workflow_orchestration.enabled (default OFF)."""
        self._workflow_runner = runner

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
        self._recent_dev_commands.append(text)
        if len(self._recent_dev_commands) > 10:
            self._recent_dev_commands = self._recent_dev_commands[-10:]

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
        """Load approval_config.json; returns {} on failure (safe defaults used by callers)."""
        try:
            import json
            from pathlib import Path
            p = Path(__file__).parent.parent / "approval_config.json"
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _capability_summary(self) -> str:
        """A concise spoken summary of what the user can ask for (GAP-4).

        Static core abilities plus the DYNAMIC skill intents from the registry
        and proactivity/personal-KB availability — so the summary never goes
        stale as skills are added by manifest.
        """
        parts = [
            "I can control the desktop: click, scroll, type, open and close apps, "
            "take screenshots, and dictate.",
            "I can write code, run plans, and answer technical questions.",
            "You can set reminders, like: every morning at 8, brief me. "
            "Say 'what are my reminders' to review them.",
        ]
        if self._personal_kb is not None and getattr(self._personal_kb, "available", False):
            parts.append("I can search your own documents — ask things like: "
                         "what did I write in my notes about a topic.")
        if self._skill_registry is not None and self._skill_registry.has_skills():
            kws: list[str] = []
            for action in self._skill_registry.list_actions()[:6]:
                if action.get("keywords"):
                    kws.append(action["keywords"][0])
            if kws:
                parts.append("Connected skills also let you say: " + "; ".join(kws) + ".")
        parts.append("Say 'pain day on' when you're flaring and I'll adapt.")
        return " ".join(parts)

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

    async def _handle_google_connect(self) -> dict:
        """Start the Google OAuth consent flow by voice (setup or recovery).

        Speaks any setup blocker (libraries / client secret) with the exact fix;
        otherwise launches the browser consent flow in the background and, on
        success, hot-starts the google_pim skill — no agent restart.
        """
        from skills import google_setup
        blocker = google_setup.setup_blocker()
        if blocker:
            asyncio.create_task(self._tts_speak(blocker))
            return {"status": "ok", "action": "GOOGLE_CONNECT_BLOCKED",
                    "reason": blocker}
        from core.async_utils import fire_and_log
        asyncio.create_task(self._tts_speak(
            "Opening Google sign-in in your browser — approve access there, "
            "and I'll tell you when it's done."))
        fire_and_log(self._google_connect_flow(), log, label="google connect flow")
        return {"status": "ok", "action": "GOOGLE_CONNECT_STARTED"}

    async def _google_connect_flow(self) -> None:
        """Background half of the connect flow: await consent, hot-start, report."""
        from skills.google_setup import run_auth_flow
        ok, message = await run_auth_flow()
        if ok and self._skill_registry is not None:
            try:
                started = await self._skill_registry.start_skill("google_pim")
                if not started:
                    message += " The skill will start on the next agent launch."
            except Exception as exc:
                log.warning("google_pim hot-start failed: %s", exc)
                message += " The skill will start on the next agent launch."
        await self._tts_speak(message)

    async def _handle_schedule_command(self, spec: Optional[dict]) -> dict:
        """Act on a parsed voice schedule / reminder / event-rule / management
        command (N+2). Writes to AgentDB; the ProactiveScheduler and
        EventRuleEngine pick the new rows up on their next tick/event."""
        if spec is None:
            asyncio.create_task(self._tts_speak(
                "Sorry, I didn't catch that. Try 'every morning at 8 brief me'."))
            return {"status": "ok", "action": "SCHEDULE_UNPARSED"}
        db = self._agent_db
        if not (db and db.available):
            return {"status": "ok", "action": "SCHEDULE_NOOP"}
        kind = spec.get("kind")

        if kind == "schedule":
            key = f"sched:{spec['goal'][:60]}:{spec['execute_at']:.0f}"
            await db.enqueue_scheduled_goal(
                spec["goal"], execute_at=spec["execute_at"],
                recurrence=spec.get("recurrence"), source_trigger="schedule",
                idempotency_key=key)
            asyncio.create_task(self._tts_speak(spec.get("spoken", "Reminder set.")))
            return {"status": "ok", "action": "SCHEDULE_SET", "goal": spec["goal"]}

        if kind == "event_rule":
            await db.insert_event_rule(
                topic_pattern=spec["topic_pattern"], goal_template=spec["goal_template"],
                name=spec.get("name"), predicate=spec.get("predicate"),
                action_kind=spec.get("action_kind", "notify"))
            asyncio.create_task(self._tts_speak(spec.get("spoken", "Rule set.")))
            return {"status": "ok", "action": "EVENT_RULE_SET"}

        if kind == "list":
            scheds = await db.list_schedules()
            rules = await db.list_event_rules()
            total = len(scheds) + len(rules)
            if not total:
                msg = "You have no reminders set."
            else:
                parts = [s["goal"][:40] for s in scheds[:5]]
                parts += [(r.get("name") or r["topic_pattern"]) for r in rules[:5]]
                msg = f"You have {total} reminder{'s' if total != 1 else ''}: " + "; ".join(parts) + "."
            asyncio.create_task(self._tts_speak(msg))
            return {"status": "ok", "action": "SCHEDULE_LIST", "count": total}

        if kind == "cancel":
            q = (spec.get("query") or "").strip().lower()
            cancelled = 0
            for s in await db.list_schedules():
                if not q or q in s["goal"].lower():
                    if await db.cancel_schedule(s["id"]):
                        cancelled += 1
            asyncio.create_task(self._tts_speak(
                f"Cancelled {cancelled} reminder{'s' if cancelled != 1 else ''}."))
            return {"status": "ok", "action": "SCHEDULE_CANCEL", "count": cancelled}

        return {"status": "ok", "action": "SCHEDULE_NOOP"}

    async def _audit_history_summary(self, n: int = 5) -> str:
        """Query audit.db for the last `n` mcp_call events and return a spoken summary."""
        if self._audit is None:
            return "Audit log not available."
        try:
            rows = await self._audit.get_recent_mcp_calls(n=n)
            if not rows:
                return "No recent actions recorded."
            parts = []
            for r in rows:
                tool = r.get("tool", "unknown")
                params = r.get("params") or ""
                # Extract the most readable param for speech
                try:
                    import json as _json
                    p = _json.loads(params) if isinstance(params, str) else params
                    detail = (
                        p.get("command", "")[:30] or
                        p.get("text", "")[:30] or
                        p.get("title_substring", "")[:30] or
                        p.get("key", "") or ""
                    )
                except Exception:
                    detail = ""
                parts.append(f"{tool}{' ' + detail if detail else ''}")
            return "Last " + str(len(parts)) + " actions: " + ", ".join(parts) + "."
        except Exception as exc:
            log.debug("HybridCoordinator._audit_history_summary failed: %s", exc)
            return "Could not retrieve history."

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
        """Route a Command through the gate decision tree and execute it.

        Dev-domain queries (code, math, vision, plan, general) are intercepted
        here and forwarded to DevAgent before the accessibility pipeline runs.
        """
        # Cross-layer trace: set the current-trace ContextVar so every awaited
        # descendant (router, executor) attaches spans without new params. Runs
        # as its own scheduler task, so no reset is needed. No-op unless DA_TRACE.
        _tracer = get_tracer()
        if _tracer.enabled:
            _tid = cmd.trace_id or _tracer.new_trace(source=cmd.source)
            if not cmd.trace_id:
                cmd.trace_id = _tid
            _tracer.set_current(_tid)

        # De-glue an ASR-concatenated leading command verb ("OpenVSCode" ->
        # "open VSCode") so a mish-spaced launch command still classifies as the
        # command domain and reaches the OPEN/CLOSE action path instead of the
        # free-form dev agent. No-op unless the text starts with a launch verb
        # glued on a camelCase boundary. Skip bypass sources (touch/multimodal),
        # whose text is a pre-resolved token, not a transcript.
        if cmd.source not in _BYPASS_SOURCES:
            from core.domain_classifier import deglue_command_verb
            deglued = deglue_command_verb(cmd.text)
            if deglued != cmd.text:
                log.info("HybridCoordinator: de-glued command verb %r -> %r",
                         cmd.text, deglued)
                cmd.text = deglued

        # --- Self-skilling rung 2: macro replay + "save that as ..." approval ---
        # Runs before the dev pre-gate so a saved macro routes by its own name
        # rather than being classified to an LLM. Voice-only; built-in
        # system-control phrases still take priority (see _maybe_handle_macro).
        if self._macro_store is not None and cmd.source in ("voice", "voice_local"):
            macro_result = await self._workflow.maybe_handle_macro(cmd)
            if macro_result is not None:
                return macro_result

        # --- Voice multi-agent workflow trigger ("think hard about …") ---
        # Decompose the goal → fan out to fresh-context sub-agents → synthesize +
        # speak one answer. Pure talk, no desktop actions; the entire command/dev
        # pipeline below is bypassed for a handled request. Default OFF (the
        # runner gates on workflow_orchestration.enabled). Fail-safe: a miss, a
        # flare, or any error returns None → ordinary routing below.
        if (self._workflow_runner is not None and self._workflow_runner.enabled
                and cmd.source in ("voice", "voice_local")):
            wf_result = await self._workflow.maybe_handle_workflow(cmd)
            if wf_result is not None:
                return wf_result

        # --- Voice conversation mode: wake/sleep-gated talk-only dialogue ---
        # When active, every voice utterance (other than the sleep phrase) is a
        # conversational turn answered by the resident general model — the entire
        # command/dev pipeline below is bypassed. Default OFF (experimental).
        if self._conv_mode.enabled and cmd.source in ("voice", "voice_local"):
            conv_result = await self._workflow.maybe_handle_conversation(cmd)
            if conv_result is not None:
                return conv_result

        # --- Dev-agent pre-gate: intercept non-command domains ---
        # Skip for voice system-control keywords so they reach the keyword block
        # below instead of being misrouted to an LLM (e.g. "pain day on").
        # Skip for bypass sources (touch / multimodal): these arrive
        # with a concrete accessibility action already resolved (e.g. a tilt-tap is
        # source="touch" action="CLICK" text="tilt_tap"). Classifying their text
        # would send "tilt_tap" to the DevAgent as a general-domain query and the
        # click would never fire — they must fall through to the gate-bypass path.
        if (
            self._dev_agent
            and not _is_system_control_voice(cmd)
            and cmd.source not in _BYPASS_SOURCES
        ):
            domain = self._get_domain_classifier().classify(cmd.text)
            if domain != "command":
                # GAP-6: anchor/track session intent for dev-domain commands.
                self._note_intent_drift(cmd)
                # Privacy: the dev pre-gate runs BEFORE the command-path Gate 0,
                # so enforce Gate 0 here too — sensitive text (credentials/PII)
                # must never reach the cloud. A Gate-0 failure forces the LOCAL
                # DevAgent regardless of the cloud-routing preference.
                route_cloud = self._should_route_cloud_dev()
                if domain == "skill":
                    # Skills execute LOCALLY via the SkillRegistry (the cloud dev
                    # agent has no registry) — never route a skill to the cloud.
                    route_cloud = False
                if _is_personal_query(cmd.text):
                    # Questions about the user's OWN documents answer from the
                    # local PersonalKB — the query itself must never go to cloud.
                    route_cloud = False
                if route_cloud and not self._gates.gate0(cmd):
                    log.info("HybridCoordinator: dev-domain=%s contains sensitive "
                             "data — forcing LOCAL DevAgent (Gate 0)", domain)
                    route_cloud = False

                # Cloud DevAgent branch — route to Claude when configured to,
                # avoiding a 30B specialist wake (and the GPU teardown it forces).
                if route_cloud:
                    # Scrub secrets/PII from the query AND the recent-command
                    # context before any cloud egress (the command-path cloud
                    # route scrubs in _run_cloud; this path bypasses it).
                    clean_text = cmd.text
                    recent = list(self._recent_dev_commands)
                    if self._content_filter:
                        clean_text, findings = await self._content_filter.scrub(cmd.text)
                        if findings:
                            log.info("ContentFilter: redacted %d secret(s) before "
                                     "cloud dev call", len(findings))
                        recent = [
                            (await self._content_filter.scrub(rc))[0] for rc in recent
                        ]
                    log.info("HybridCoordinator: dev-domain=%s → CloudDevAgent (%s)",
                             domain, getattr(self._cloud_dev_agent, "model", "?"))
                    ctx = {
                        "session_id": self._session_id,
                        "recent_commands": recent,
                        "source": cmd.source,
                        "trace_id": cmd.trace_id,
                    }
                    self._gates.note_cloud_call()  # GAP-10: denial-of-wallet tripwire
                    # Clear the task-local capture so a stale prompt/token count
                    # from a prior inference in this task can't be attributed to
                    # this cloud-dev call (mirrors _run_cloud).
                    set_inference_capture(None)
                    response_text = await self._cloud_dev_agent.run(clean_text, domain, ctx)
                    # Persist the Opus-tier token usage for the cost ledger. This
                    # is the most expensive cloud path; CloudDevAgent.run() set the
                    # capture from the Bedrock response usage.
                    if self._agent_db and self._agent_db.available:
                        _p, _ti, _to = get_inference_capture()
                        _cda_model = getattr(self._cloud_dev_agent, "model", "unknown")
                        await self._agent_db.insert_inference(
                            command_id=None,
                            model=_cda_model,
                            domain=domain,
                            prompt=None,
                            response=None,
                            tokens_in=_ti,
                            tokens_out=_to,
                            latency_ms=0.0,
                            backend="bedrock",
                            error=None,
                        )
                    # Record the ORIGINAL locally (kept on-device; re-scrubbed at
                    # the next cloud egress).
                    self._record_dev_command(cmd.text)
                    if self._twin:
                        self._twin.clear_dev_namespace()
                    return {
                        "status": "ok",
                        "action": "dev_agent",
                        "domain": domain,
                        "model": self._cloud_dev_agent.model,
                        "response": response_text,
                        "steps": 0,
                        "backend": "bedrock",
                    }

                log.info("HybridCoordinator: dev-domain=%s → DevAgent", domain)
                # Chat file attachments (specs/chat-context-attachments R2.4): the
                # chat server stuffs extracted context + an optional image into
                # cmd.params. Forward both; absent → byte-identical to today.
                _att_ctx = cmd.params.get("attachment_context", "") if cmd.params else ""
                _att_img = cmd.params.get("attachment_image_b64") if cmd.params else None
                agent_result = await self._dev_agent.handle(
                    cmd.text, screenshot_b64=_att_img, trace_id=cmd.trace_id,
                    attachment_context=_att_ctx)
                # Personal-document queries are NOT recorded in the rolling dev
                # context: _recent_dev_commands is sent verbatim to the cloud
                # dev agent on later queries, which would leak the very text the
                # force-local guard kept on-device.
                if not _is_personal_query(cmd.text):
                    self._record_dev_command(cmd.text)
                if self._twin:
                    self._twin.clear_dev_namespace()
                return {
                    "status": "ok",
                    "action": "dev_agent",
                    "domain": agent_result.domain,
                    "model": agent_result.model_used,
                    "response": agent_result.response_text,
                    "steps": len(agent_result.steps),
                    "backend": "local",
                }

        # System control commands — intercept before gate evaluation
        if cmd.source in ("voice", "voice_local"):
            # Strip surrounding whitespace AND punctuation: Whisper routinely
            # appends a period ("pain day on." / "lecture mode on?") which would
            # otherwise miss every exact-match keyword below.
            _lower = cmd.text.lower().strip(" \t\n.,!?;:\"'")

            # Lecture mode
            if _lower in ("start lecture mode", "lecture mode on", "begin lecture mode"):
                self._lecture_mode = True
                if self._whisper:
                    self._whisper.set_lecture_mode(True)
                log.info("Lecture mode ON")
                return {"status": "ok", "action": "LECTURE_MODE", "enabled": True}
            elif _lower in ("stop lecture mode", "lecture mode off", "end lecture mode"):
                self._lecture_mode = False
                if self._whisper:
                    self._whisper.set_lecture_mode(False)
                log.info("Lecture mode OFF")
                return {"status": "ok", "action": "LECTURE_MODE", "enabled": False}

            # Lecture notes search — "search my lecture notes for X"
            elif "lecture notes" in _lower and (
                _lower.startswith("search") or "search" in _lower
            ):
                # Extract query after "for" or "about"
                for sep in ("for ", "about ", "on "):
                    if sep in _lower:
                        search_q = cmd.text[_lower.index(sep) + len(sep):].strip()
                        break
                else:
                    search_q = cmd.text  # fallback: search whole phrase
                if self._agent_db and self._agent_db.available and search_q:
                    rows = await self._agent_db.search_lecture_notes(search_q, limit=10)
                    if rows:
                        summary = "\n".join(f"- {r['text']}" for r in rows[:5])
                        log.info("Lecture notes search %r: %d results", search_q, len(rows))
                        return {"status": "ok", "action": "LECTURE_SEARCH",
                                "query": search_q, "results": len(rows),
                                "preview": summary}
                    else:
                        return {"status": "ok", "action": "LECTURE_SEARCH",
                                "query": search_q, "results": 0,
                                "preview": "No lecture notes found for that query."}

            # Manual pain day toggle
            elif _lower in ("pain day on", "flare day on", "bad day"):
                if self._twin:
                    self._twin.set_manual_pain_day(True)
                # Immediately relax the recognizer (VAD + logprob floor) for the
                # next utterance, before route() reconciles on the next command.
                if self._whisper is not None:
                    self._whisper.apply_pain_day(True)
                return {"status": "ok", "action": "PAIN_DAY", "enabled": True}
            elif _lower in ("pain day off", "flare day off", "feeling better"):
                if self._twin:
                    self._twin.set_manual_pain_day(False)
                if self._whisper is not None:
                    self._whisper.apply_pain_day(False)
                return {"status": "ok", "action": "PAIN_DAY", "enabled": False}

            # Condition switching — loads calibrated voice profile for condition
            _CONDITION_TRIGGERS: dict[str, str] = {
                "this is a good day":      "good_day",
                "good day mode":           "good_day",
                "feeling well":            "good_day",
                "this is a flare day":     "flare_day",
                "flare day":               "flare_day",
                "flare mode":              "flare_day",
                "this is an allergy day":  "allergy_day",
                "allergy day":             "allergy_day",
                "allergy mode":            "allergy_day",
            }
            if _lower in _CONDITION_TRIGGERS and self._profiler:
                condition = _CONDITION_TRIGGERS[_lower]
                t = asyncio.create_task(self._switch_condition(condition))
                t.add_done_callback(lambda t: self._on_task_done(t, "_switch_condition"))
                return {"status": "ok", "action": "CONDITION_SWITCH",
                        "condition": condition}

            # Calibration triggers
            _CALIBRATION_TRIGGERS: dict[str, tuple[str, bool]] = {
                "run voice calibration":   ("good_day",    False),
                "calibrate my voice":      ("good_day",    False),
                "quick calibration":       ("good_day",    True),
                "calibrate flare day":     ("flare_day",   False),
                "calibrate allergy day":   ("allergy_day", False),
            }
            if _lower in _CALIBRATION_TRIGGERS and self._calibrator:
                condition, quick = _CALIBRATION_TRIGGERS[_lower]
                t = asyncio.create_task(self._run_calibration(condition, quick))
                t.add_done_callback(lambda t: self._on_task_done(t, "_run_calibration"))
                return {"status": "ok", "action": "CALIBRATION_START",
                        "condition": condition, "quick": quick}

            # ── Goal-level agent control ──────────────────────────────────────

            # "hey agent status" / "what are you doing"
            if _lower in ("hey agent status", "what are you doing", "agent status",
                          "status", "what's happening"):
                if self._dev_agent is not None:
                    ps = self._dev_agent.get_plan_status()
                    if ps.get("active"):
                        msg = (f"Running step {ps['step']} of {ps['total_steps']}: "
                               f"{ps.get('goal', '')[:50]}")
                    else:
                        msg = "No active task."
                    asyncio.create_task(self._tts_speak(msg))
                    return {"status": "ok", "action": "AGENT_STATUS", "plan": ps}

            # "hey agent stop" / "cancel task" / "cancel agent"
            elif _lower in ("hey agent stop", "cancel task", "cancel agent", "stop agent",
                            "stop the agent", "cancel the task"):
                if self._dev_agent is not None:
                    self._dev_agent.request_cancel()
                    asyncio.create_task(self._tts_speak("Cancelling after current step."))
                    return {"status": "ok", "action": "AGENT_CANCEL"}
                return {"status": "ok", "action": "AGENT_CANCEL", "note": "no active agent"}

            # "resume task" — offer to resume the most recent interrupted plan
            # (post-crash recovery; advertised by the crash-notice TTS in
            # main.py). Safe to fire-and-forget: resume_pending_plan() is
            # itself gated on an explicit spoken confirmation, so this phrase
            # alone can never re-run a plan with destructive steps.
            elif _lower in ("resume task", "resume the task", "hey agent resume",
                            "resume work", "resume interrupted task"):
                pending: list = []
                if self._agent_db and self._agent_db.available:
                    pending = await self._agent_db.get_interrupted_runs(limit=1)
                if self._dev_agent is None or not pending:
                    asyncio.create_task(self._tts_speak("No interrupted task to resume."))
                    return {"status": "ok", "action": "AGENT_RESUME", "offered": False}
                t = asyncio.create_task(self._dev_agent.resume_pending_plan())
                t.add_done_callback(lambda t: self._on_task_done(t, "resume_pending_plan"))
                return {"status": "ok", "action": "AGENT_RESUME", "offered": True,
                        "goal": pending[0].get("goal", "")[:80]}

            # "hey agent authorize <goal>" — create a standalone goal session
            elif _lower.startswith("hey agent authorize ") or _lower.startswith("authorize "):
                goal_text = (cmd.text.split("authorize ", 1)[-1]).strip(" .,!?\"'")
                if goal_text:
                    from core.goal_session import GoalSessionStore
                    duration = self._approval_config().get("goal_session_duration_s", 900)
                    max_act = self._approval_config().get("goal_session_max_actions", 50)
                    GoalSessionStore.create(goal=goal_text, domain="plan",
                                            duration_s=duration, max_actions=max_act)
                    # Durable goal backlog (gap D): persist the goal so it survives a
                    # crash/restart, then kick the drainer to run it. idempotency_key
                    # is unique per authorize so a re-issue can't double-queue.
                    if self._agent_db and self._agent_db.available:
                        import time as _t
                        key = f"authorize:{goal_text[:80]}:{_t.time():.0f}"
                        await self._agent_db.enqueue_goal(
                            goal_text, domain="plan", idempotency_key=key,
                        )
                        if self._dev_agent is not None:
                            asyncio.create_task(self._dev_agent.drain_goal_queue())
                    mins = int(duration / 60)
                    asyncio.create_task(
                        self._tts_speak(f"Goal authorized for {mins} minutes: {goal_text[:40]}")
                    )
                    return {"status": "ok", "action": "GOAL_AUTHORIZE", "goal": goal_text}

            # ── Proactive scheduling / reminders / event rules (N+2) ──────────
            elif is_schedule_phrase(_lower):
                import time as _t
                return await self._handle_schedule_command(
                    parse_schedule(cmd.text, _t.time()))

            # "hey agent history" / "what did you do"
            elif _lower in ("hey agent history", "what did you do", "agent history",
                            "show history", "recent actions"):
                summary = await self._audit_history_summary(n=5)
                asyncio.create_task(self._tts_speak(summary))
                return {"status": "ok", "action": "AGENT_HISTORY", "summary": summary}

            # "review queue" / "what needs review" — dev plans that exhausted
            # their replan/step budget, were rolled back, and now need a human
            # decision (R-10 escalation queue)
            elif _lower in ("review queue", "show review queue", "what needs review",
                            "hey agent review queue", "show escalations",
                            "pending reviews"):
                items: list = []
                total = 0
                if self._agent_db and self._agent_db.available:
                    total = await self._agent_db.count_pending_escalations()
                    items = await self._agent_db.get_pending_escalations(limit=5)
                if total:
                    newest = items[0]
                    msg = (f"{total} plan{'s' if total != 1 else ''} need review. "
                           f"Most recent: {newest['goal'][:50]}, "
                           f"{newest['reason'].replace('_', ' ')}.")
                else:
                    msg = "Review queue is empty."
                asyncio.create_task(self._tts_speak(msg))
                return {"status": "ok", "action": "AGENT_ESCALATIONS",
                        "count": total, "items": items}

            # "clear review queue" — acknowledge every pending escalation
            elif _lower in ("clear review queue", "dismiss reviews",
                            "clear escalations"):
                cleared = 0
                if self._agent_db and self._agent_db.available:
                    cleared = await self._agent_db.resolve_escalations(
                        status="acknowledged")
                asyncio.create_task(self._tts_speak(
                    f"Cleared {cleared} review item{'s' if cleared != 1 else ''}."))
                return {"status": "ok", "action": "AGENT_ESCALATIONS_CLEAR",
                        "count": cleared}

            # Mic mute — voice one-way; unmute via iPad mic_mute message
            elif _lower in ("mute mic", "mute microphone", "mic off", "silence mic"):
                if self._whisper is not None:
                    self._whisper.set_muted(True)
                asyncio.create_task(self._tts_speak("Microphone muted. Tap the iPad to unmute."))
                return {"status": "ok", "action": "MIC_MUTE", "muted": True}

            # Capability discovery — "help" / "what can you do" (GAP-4)
            elif _lower in ("help", "what can you do", "what can i say",
                            "list your skills"):
                summary = self._capability_summary()
                asyncio.create_task(self._tts_speak(summary))
                return {"status": "ok", "action": "HELP", "summary": summary}

            # Personal KB maintenance — "index my notes"
            elif _lower in ("index my notes", "reindex my notes",
                            "index my documents"):
                if self._personal_kb is not None and getattr(
                        self._personal_kb, "available", False):
                    from core.async_utils import fire_and_log
                    if self._personal_kb.get_status().get("paused"):
                        asyncio.create_task(self._tts_speak(
                            "Indexing is paused during the flare — I'll be able "
                            "to index after it passes."))
                        return {"status": "ok", "action": "PERSONAL_KB_PAUSED"}
                    fire_and_log(self._personal_kb.index(), log,
                                 label="voice personal_kb index")
                    asyncio.create_task(self._tts_speak(
                        "Okay, indexing your documents in the background."))
                    return {"status": "ok", "action": "PERSONAL_KB_INDEX"}
                asyncio.create_task(self._tts_speak(
                    "The personal knowledge base isn't available."))
                return {"status": "ok", "action": "PERSONAL_KB_UNAVAILABLE"}

            # Google PIM auth — "connect google" / "reconnect google".
            # One spoken phrase + one browser consent click replaces the old
            # env-var + script + manifest-edit setup; also the recovery path
            # the expired-token messages name.
            elif _lower in ("connect google", "reconnect google",
                            "connect gmail", "set up gmail", "set up google"):
                return await self._handle_google_connect()

        t0 = time.monotonic()
        route_label = "local"
        gate_that_decided = "all_pass"
        action_str: Optional[str] = None
        success: Optional[bool] = None
        error_msg: Optional[str] = None
        command_id: int = -1
        # Per-command accumulator for inference row ids written by
        # _run_local/_run_cloud BEFORE the command row exists. Task-local
        # (ContextVar) so concurrent route() tasks never share a list. The ids
        # are backfilled onto inferences.command_id right after insert_command.
        _inf_ids_token = _PENDING_INFERENCE_IDS.set([])

        try:
            source = cmd.source

            # --- Twin state snapshot and adjustments -----------------------
            from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
            snapshot = _DEFAULT_SNAPSHOT
            if self._twin:
                try:
                    snapshot = await self._twin.get_snapshot()
                except Exception as exc:
                    log.warning("BehavioralTwinState.get_snapshot failed: %s", exc)

            # Apply pain-day threshold adjustments
            if snapshot.pain_day_active:
                effective_cfg = _apply_pain_day_adjustments(self._cfg, snapshot)
            else:
                effective_cfg = self._cfg

            # Propagate pain-day state to sensor + voice thresholds. Each
            # consumer relaxes only if its flare_profile degrade flag is set,
            # and apply_pain_day is idempotent so calling every command is cheap.
            if self._fusion is not None:
                self._fusion.apply_pain_day(
                    tilt=snapshot.pain_day_active and snapshot.flare_tilt_degrades,
                )
            if self._whisper is not None:
                self._whisper.apply_pain_day(
                    snapshot.pain_day_active and snapshot.flare_voice_degrades
                )

            # Always apply vocabulary corrections before any gate evaluation
            # so app-name phonetics ("key-row" → "vscode") reach the LLM fixed
            # regardless of Whisper confidence level.
            vocab_corrected = False
            if cmd.source in ("voice", "voice_local"):
                corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
                if changed:
                    log.debug(
                        "Pre-gate vocab correction: %r → %r", cmd.text, corrected_text
                    )
                    cmd = _dc_replace(cmd, text=corrected_text)
                    vocab_corrected = True

            # Populate session_context from twin state (always accessibility namespace)
            if self._twin and self._twin.is_ready:
                cmd = _dc_replace(cmd, session_context=self._twin.get_session_context("accessibility"))

            # Conversational continuity (voice only): deterministically resolve
            # anaphora ("do that again", "click it") against the previous resolved
            # turn BEFORE inference — the local 8B model is poor at this — and
            # append one structured last-action line so both the local and cloud
            # prompts see what actually happened, not just what was said.
            if cmd.source in ("voice", "voice_local", "voice_correction"):
                resolved_text, changed = self._conversation.resolve_anaphora(cmd.text)
                if changed:
                    log.debug("Anaphora resolved: %r → %r", cmd.text, resolved_text)
                    cmd = _dc_replace(cmd, text=resolved_text)
            hint = self._conversation.prompt_hint()
            if hint:
                cmd = _dc_replace(
                    cmd, session_context=list(cmd.session_context or []) + [hint]
                )

            # Inject pending clarification so the LLM knows what "up" or "yes"
            # is answering.  Prepended so it appears closest to the user turn.
            if self._pending_clarification and cmd.source in ("voice", "voice_local"):
                clarify_ctx = f"[PENDING CLARIFICATION: {self._pending_clarification}]"
                ctx = [clarify_ctx] + list(cmd.session_context or [])
                cmd = _dc_replace(cmd, session_context=ctx)

            # Temporarily apply effective_cfg for gate evaluation
            _original_cfg = self._cfg
            self._cfg = effective_cfg
            try:
                # --- Bypass path (touch / multimodal) --------------------------
                # These sources always run local and never reach the cloud, so
                # Gate 0 — whose sole purpose is to keep sensitive text off an
                # external API — does not apply and is checked AFTER this branch.
                # Touch commands (iPad CommandPad taps, tilt-tap) arrive with a
                # concrete action already resolved — the text is just a label
                # ("tilt_tap"). Honor that action directly instead of asking the
                # LLM to infer it from the label, which yields CLARIFY ("What is
                # the target of the click?") because the model can't read
                # "tilt_tap" as a verb. Multimodal still infers via the LLM
                # (its action depends on which phrase fired).
                if source in _BYPASS_SOURCES:
                    if cmd.source == "touch" and cmd.action in _VALID_COMMAND_VERBS:
                        action_str = cmd.action
                    else:
                        action_str = await self._inference.run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "bypass"

                # --- Gate 0 — Privacy (force local for cloud-eligible sources) --
                elif not self._gates.gate0(cmd):
                    log.debug("Gate 0 force-local (sensitive data): %r", cmd.text)
                    action_str = await self._inference.run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "gate0_privacy"

                # --- Skip Gate 1 path ------------------------------------------
                elif source in _SKIP_GATE1_SOURCES:
                    action_str, gate_that_decided, route_label = await self._gates.gates_2_to_4(cmd)

                # --- Full 4-gate path -------------------------------------------
                else:
                    # Gate 1 — Confidence
                    passed, cmd = await self._gates.gate1(cmd)
                    if passed is None:
                        # Gesture low confidence — discard. Record it (E10) so the
                        # drop is visible to retraining and analytics instead of
                        # vanishing with only a debug line.
                        log.debug("Gate 1 discard (low gesture conf): %r", cmd.text)
                        if self._agent_db and self._agent_db.available:
                            try:
                                await self._agent_db.insert_command(
                                    session_id=self._session_id,
                                    cmd=cmd,
                                    action="DISCARDED",
                                    route="local",
                                    gate_that_decided="gate1_gesture_conf",
                                    latency_ms=(time.monotonic() - t0) * 1000,
                                    success=False,
                                    error_msg="low gesture confidence",
                                    trace_id=cmd.trace_id or None,
                                )
                            except Exception as _disc_exc:
                                log.debug("discard log failed: %s", _disc_exc)
                        return {"status": "discarded", "reason": "gate1_gesture_conf"}
                    if not passed:
                        # Voice low confidence. If the pre-gate vocabulary pass
                        # already fixed a KNOWN misrecognition the transcript is now
                        # high-confidence — continue local (fast, no round-trip).
                        # Otherwise it's an UNKNOWN low-confidence utterance:
                        # escalate to the cloud, whose system prompt is tuned to
                        # repair voice misrecognitions the local dictionary can't.
                        # Gate 0 has already passed here, so no sensitive data is
                        # transmitted.
                        if vocab_corrected:
                            cmd = await _retranscribe(cmd)
                            action_str, gate_that_decided, route_label = \
                                await self._gates.gates_2_to_4(cmd)
                        else:
                            log.info(
                                "Gate 1 voice low-confidence (logprob=%.3f) — "
                                "escalating to cloud for misrecognition repair",
                                cmd.whisper_logprob,
                            )
                            action_str = await self._inference.run_cloud(cmd)
                            gate_that_decided = "gate1_voice_conf"
                            route_label = "cloud"
                    else:
                        action_str, gate_that_decided, route_label = \
                            await self._gates.gates_2_to_4(cmd)
            finally:
                self._cfg = _original_cfg

            # --- Execute the action ----------------------------------------
            result = await self._action_executor.execute_action(action_str, cmd, route_label=route_label)
            success = result.get("status") == "ok"

            # E17: record a concrete failure reason so root-cause analysis does
            # not have to reverse-engineer it from the action column. CLARIFY
            # outcomes carry their reason text; verify_failed/resolve_miss/error
            # carry the status (or executor error).
            if not success and error_msg is None:
                if isinstance(action_str, str) and action_str.upper().startswith("CLARIFY"):
                    error_msg = action_str[len("CLARIFY"):].strip()[:200] or "clarify"
                else:
                    error_msg = result.get("error") or result.get("status")

            # Persist to DB now so command_id is valid before trainer uses it
            latency_ms = (time.monotonic() - t0) * 1000
            if self._agent_db and self._agent_db.available:
                try:
                    resolved_by_val = result.get("result", {}).get("resolved_by") if isinstance(result.get("result"), dict) else None
                    command_id = await self._agent_db.insert_command(
                        session_id=self._session_id,
                        cmd=cmd,
                        action=action_str,
                        route=route_label,
                        gate_that_decided=gate_that_decided,
                        latency_ms=latency_ms,
                        success=success,
                        error_msg=error_msg,
                        trace_id=cmd.trace_id or None,
                        resolved_by=resolved_by_val,
                    )
                except Exception as db_exc:
                    log.warning("AgentDB.insert_command failed: %s", db_exc)
                else:
                    # Backfill inferences.command_id now that the command row
                    # exists — without this the fine-tuning extraction JOIN
                    # (inferences ⨝ commands) matches nothing.
                    _inf_ids = _PENDING_INFERENCE_IDS.get()
                    if _inf_ids and command_id and command_id > 0:
                        try:
                            await self._agent_db.link_inferences_to_command(
                                _inf_ids, command_id
                            )
                        except Exception as link_exc:
                            log.debug("link_inferences_to_command failed: %s", link_exc)

            # Publish gate decision and command outcome events (fail-safe).
            if self._event_bus is not None:
                try:
                    from core.events import TOPIC_COMMAND_EXECUTED, TOPIC_GATE_DECIDED
                    _ev_payload_gate = {
                        "gate": gate_that_decided, "route": route_label,
                        "domain": getattr(cmd, "domain", None),
                        "latency_ms": round(latency_ms, 1),
                    }
                    _ev_payload_cmd = {
                        "action": action_str, "route": route_label,
                        "gate": gate_that_decided,
                        "latency_ms": round(latency_ms, 1),
                        "success": success,
                    }
                    from core.async_utils import fire_and_log
                    fire_and_log(self._event_bus.publish(
                        TOPIC_GATE_DECIDED, _ev_payload_gate, source="coordinator",
                        session_id=self._session_id, command_id=command_id,
                        trace_id=cmd.trace_id or None,
                    ))
                    fire_and_log(self._event_bus.publish(
                        TOPIC_COMMAND_EXECUTED, _ev_payload_cmd, source="coordinator",
                        session_id=self._session_id, command_id=command_id,
                        trace_id=cmd.trace_id or None,
                    ))
                    # Update bridge's active trace_id so ipad_log entries
                    # arriving within the 2 s window are correlated to this command.
                    if self._bridge is not None and cmd.trace_id:
                        self._bridge.set_active_trace_id(cmd.trace_id)
                except Exception as _ev_exc:
                    log.debug("HybridCoordinator: event publish failed: %s", _ev_exc)

            # Record successful local executions for few-shot learning
            if (self._trainer and route_label == "local" and success):
                await self._trainer.record_success(
                    cmd, action_str, command_id=command_id
                )
            # Feed failed local executions into the twin's pain-day fail signal
            # and the counterexample store (guarded — see trainer.record_failure;
            # never the positive few-shot store). CLARIFY executes with status
            # "ok" so it is a success here, not a failure. Cloud outcomes stay
            # out of the twin, matching the local-only success gate above.
            elif (self._trainer and route_label == "local" and not success):
                await self._trainer.record_failure(
                    cmd, action_str, command_id=command_id
                )

            # D3: drain gesture velocity samples after every gesture command
            # that cleared Gate 1, regardless of execution outcome.
            if self._trainer and cmd.source == "gesture":
                await self._trainer.drain_and_persist_velocity(
                    pain_day=snapshot.pain_day_active
                )

            # D8: handle voice corrections — record the right action for
            # commands where the user said "no/wait/actually <new command>"
            if cmd.source == "voice_correction":
                await self._on_correction(cmd, action_str)

            # D8: record action/status so WhisperStream can detect next correction
            self._last_executed_action = action_str or ""
            self._last_command_id = command_id
            if self._whisper:
                status_str = "ok" if success else ("CLARIFY" if action_str == "CLARIFY" else "failed")
                self._whisper.set_last_command_status(status_str, cmd.text)

            # Advance acoustic profiler command counter (seasonal drift check)
            if self._whisper and hasattr(self._whisper, "_profiler") \
                    and self._whisper._profiler:
                self._whisper._profiler.on_any_command()

        except Exception as exc:
            log.error("HybridCoordinator.route error: %s", exc)
            error_msg = str(exc)
            return {"status": "error", "error": str(exc)}

        finally:
            _PENDING_INFERENCE_IDS.reset(_inf_ids_token)
            latency_ms = (time.monotonic() - t0) * 1000
            # Gate 4's EMA exists to detect when LOCAL inference is getting slow
            # (e.g. GPU contention during a flare) and shed load to the cloud.
            # Feeding cloud round-trip latency — inherently several times the
            # local budget — back into it would inflate the EMA, trip Gate 4, and
            # push even more commands to the cloud: a positive feedback loop that
            # never recovers. So only local routes update the Gate 4 EMA; when a
            # burst goes to cloud the EMA stays low and local is retried promptly.
            if route_label == "local":
                self._gates.update_ema(latency_ms)
            # Record outcome in metrics singleton (non-fatal)
            if self._metrics is not None:
                try:
                    self._metrics.record_command_outcome(
                        success=success,
                        action=action_str or "",
                        latency_ms=latency_ms,
                        route=route_label,
                        domain=getattr(cmd, "domain", None),
                        gate=gate_that_decided,
                        whisper_logprob=getattr(cmd, "whisper_logprob", None),
                        gesture_conf=getattr(cmd, "gesture_confidence", None),
                    )
                except Exception:
                    pass
            # Cross-layer trace: the decisive routing span (no-op unless DA_TRACE)
            try:
                _tracer.record_span(
                    "route_decision", route=route_label, gate=gate_that_decided,
                    action=action_str or None, success=success,
                    dur_ms=round(latency_ms, 1),
                )
            except Exception:
                pass

        return result

    # ---------------------------------------------------------------------- #
    # Gate implementations
    # ---------------------------------------------------------------------- #

    async def _on_correction(self, cmd: Command, correct_action: str) -> None:
        """D8: Record a voice correction as a few-shot example.

        Called when the user says "no/wait/actually <new command>" after a
        failed or CLARIFY outcome.  The corrected text becomes the canonical
        example for future routing of similar commands.
        """
        # GAP-9: harvest the correction regardless of whether a trainer is wired —
        # the user_corrections backlog must capture every confirmed correction,
        # not only those on deploys that also run a ContinuousTrainer.
        self._harvest_correction(cmd, self._last_executed_action)
        # S3.1: Write the gold label to the commands table so it can be harvested
        if self._agent_db and self._agent_db.available and self._last_command_id is not None:
            from core.async_utils import fire_and_log
            fire_and_log(
                self._agent_db.mark_command_corrected(self._last_command_id, correct_action),
                log, label="mark command corrected"
            )

        if not self._trainer:
            return
        try:
            await self._trainer.record_correction(
                cmd=cmd,
                wrong_action=self._last_executed_action,
                correct_action=correct_action,
                command_id=self._last_command_id,
            )
            log.info(
                "HybridCoordinator: correction recorded %r → %r",
                self._last_executed_action, correct_action,
            )
        except Exception as exc:
            log.warning("HybridCoordinator._on_correction failed: %s", exc)

    # GAP-6 thresholds: 3 consecutive dev turns below this token-overlap to the
    # opening intent trip a single advisory warning.
    _DRIFT_SIM_THRESHOLD = 0.3
    _DRIFT_STREAK_TRIGGER = 3

    def _note_intent_drift(self, cmd: Command) -> None:
        """GAP-6: watch a dev session for divergence from its opening intent.

        The first dev-domain command anchors `_session_intent`; each later dev
        command is scored by cheap token-overlap (Jaccard) against it. Three
        consecutive below-threshold turns log a one-time DRIFT_WARNING, persist a
        row, and speak a gentle nudge — all fire-and-forget, off the hot path.
        The command itself is NOT blocked: the signal is advisory (noisy), so it
        informs rather than hijacks. Dev-domain only.

        Scope note: the coordinator is one long-lived instance, so "session" here
        means the process lifetime — the anchor is the first dev command after
        boot and the warning latches once per process. That suits the typical
        single-goal session; re-anchoring on an idle boundary is a future option.
        """
        text = (cmd.text or "").strip()
        if not text:
            return
        if self._session_intent is None:
            self._session_intent = text
            return
        from storage.db import _tokens, _jaccard
        sim = _jaccard(_tokens(self._session_intent), _tokens(text))
        if sim >= self._DRIFT_SIM_THRESHOLD:
            self._drift_streak = 0
            return
        self._drift_streak += 1
        if self._drift_streak < self._DRIFT_STREAK_TRIGGER or self._drift_warned:
            return
        self._drift_warned = True
        log.warning(
            "HybridCoordinator: intent drift — %r diverges from opening %r (sim=%.2f)",
            text[:60], self._session_intent[:60], sim,
        )
        from core.async_utils import fire_and_log
        if self._agent_db and self._agent_db.available:
            fire_and_log(
                self._agent_db.insert_drift(
                    self._session_id, getattr(cmd, "trace_id", None), sim,
                    self._session_intent, text),
                log, label="insert drift")
        if self._audit and getattr(self._audit, "available", False):
            fire_and_log(
                self._audit.log_security_event(
                    detail=f"intent drift: command diverges from session intent (sim={sim:.2f})",
                    severity="warning",
                    params={"original_intent": self._session_intent[:120],
                            "current_command": text[:120],
                            "drift_score": round(sim, 3)}),
                log, label="audit drift")
        nudge = f"You started by asking about {self._session_intent[:48]}. Are we still on track?"
        fire_and_log(self._tts_speak(nudge), log, label="drift nudge")

    def _harvest_correction(self, cmd: Command, prior_action: str) -> None:
        """GAP-9: persist a confirmed user correction as labeled failure data.

        Fire-and-forget; reuses the explicit-correction signal (reliable) rather
        than a heuristic. scripts/cluster_corrections.py clusters these offline.
        """
        if not (self._agent_db and self._agent_db.available):
            return
        from core.async_utils import fire_and_log
        try:
            domain = self._get_domain_classifier().classify(cmd.text)
        except Exception:
            domain = None
        fire_and_log(
            self._agent_db.insert_correction(
                self._session_id, getattr(cmd, "trace_id", None),
                cmd.text or "", prior_action or "", domain),
            log, label="harvest correction")

    # ---------------------------------------------------------------------- #
    # Correction API — user feedback loop
    # ---------------------------------------------------------------------- #

    async def correct(self, cmd: Command, wrong_action: str, correct_action: str) -> dict:
        """Record a user correction for a misresolved command.

        Called when the user indicates the last action was wrong and provides
        the correct one (e.g. via iPad "undo + correct" flow or voice "no, I
        meant close").

        Args:
            cmd: The original Command that was misresolved.
            wrong_action: The action that was incorrectly executed.
            correct_action: The action the user actually wanted.

        Returns:
            Status dict with confirmation.
        """
        log.info(
            "Correction received: %r → was %s, should be %s",
            cmd.text, wrong_action, correct_action,
        )

        if self._trainer:
            await self._trainer.record_correction(cmd, wrong_action, correct_action)

        self._harvest_correction(cmd, wrong_action)

        return {
            "status": "ok",
            "correction": {
                "text": cmd.text,
                "wrong": wrong_action,
                "correct": correct_action,
            },
        }

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

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
