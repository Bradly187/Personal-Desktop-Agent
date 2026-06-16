"""adaptive.behavioral_twin_state.py — Behavioral Twin State for the Personal Desktop Agent.

Maintains a persistent model of Brad's behavioral patterns: preferred commands,
time-of-day rhythms, pain-day signals, and cross-session context.

Exposes a queryable async interface consumed by HybridCoordinator (before every
routing decision) and ContinuousTrainer (for adaptation signals).

Architecture:
    BehavioralTwinState ← ContinuousTrainer.observe(cmd, action)
    BehavioralTwinState → HybridCoordinator via TwinSnapshot (frozen)

Backing stores:
    - AgentDB (aiosqlite SQLite) — structured preference and session data
    - SemanticMemory (ChromaDB) — vector embeddings for semantic retrieval
      (optional; degrades to AgentDB-only Jaccard scoring when unavailable)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.db import AgentDB
    from core.hybrid_coordinator import Command

from storage.semantic_memory import SemanticMemory

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TwinSnapshot — immutable behavioral state snapshot (Requirement 5.2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TwinSnapshot:
    """Immutable point-in-time view of Brad's behavioral state.

    Consumed by HybridCoordinator before gate evaluation and by
    ContinuousTrainer during adaptation passes. Frozen so concurrent
    reads from multiple pipeline components are safe without locks.
    """

    pain_day_active: bool                  # PainDayMode currently active
    preferred_actions: list[str]           # Top-5 action verbs for current time window
    source_weights: dict[str, float]       # Per-source success ratio (last 7 days)
    session_context: list[str]             # Last N command texts (20 normal, 10 pain day)
    command_count_session: int             # Commands executed this session (NOT per calendar day)
    pain_day_score: float                  # Current score in [0.0, 1.0]
    snapshot_ts: float                     # time.monotonic() at creation
    # flare_profile gating — which sensors Brad said actually degrade on a
    # flare (iPad FlareProfileSheet). Consumers relax only when their flag is
    # set, so pain-day relaxation matches his real symptom pattern. Default
    # True (relax everything) until a profile is configured.
    flare_voice_degrades: bool = True
    flare_gesture_degrades: bool = True
    flare_tilt_degrades: bool = True
    flare_sound_degrades: bool = True


# Default snapshot returned before start() completes or on any error.
# Safe to use as a fallback — all fields are empty/zero/False.
_DEFAULT_SNAPSHOT = TwinSnapshot(
    pain_day_active=False,
    preferred_actions=[],
    source_weights={},
    session_context=[],
    command_count_session=0,
    pain_day_score=0.0,
    snapshot_ts=0.0,
)


@dataclass
class ActionStats:
    frequency: int = 0
    mean_confidence: float = 0.0
    mean_latency_ms: float = 0.0
    last_used_ts: float = 0.0
    # Running Welford accumulators (avoids storing all samples)
    _conf_sum: float = field(default=0.0, repr=False)
    _lat_sum: float = field(default=0.0, repr=False)
    # D7: Welford online variance for gesture confidence
    _conf_m2: float = field(default=0.0, repr=False)
    # D7: Split good-day vs pain-day confidence baselines
    good_day_conf_sum: float = field(default=0.0, repr=False)
    good_day_conf_count: int = field(default=0, repr=False)
    pain_day_conf_sum: float = field(default=0.0, repr=False)
    pain_day_conf_count: int = field(default=0, repr=False)

    @property
    def conf_variance(self) -> float:
        """Welford online variance of confidence samples."""
        return self._conf_m2 / max(self.frequency - 1, 1)

    @property
    def good_day_mean_confidence(self) -> float:
        if self.good_day_conf_count == 0:
            return self.mean_confidence
        return self.good_day_conf_sum / self.good_day_conf_count

    @property
    def pain_day_mean_confidence(self) -> float:
        if self.pain_day_conf_count == 0:
            return self.mean_confidence
        return self.pain_day_conf_sum / self.pain_day_conf_count


# ---------------------------------------------------------------------------
# PreferenceModel — structured preference tracking (Requirement 2.x)
# ---------------------------------------------------------------------------

@dataclass
class PreferenceModel:
    """Tracks per-action statistics, time-of-day rhythms, and source reliability.

    Persisted as a JSON blob in AgentDB (settings_versions table,
    component='preference_model', key='snapshot').
    """

    # Per-action-verb stats
    action_stats: dict[str, ActionStats] = field(default_factory=dict)
    # Time-of-day buckets: 0=00-06, 1=06-12, 2=12-18, 3=18-24
    # Maps bucket_index → {action_verb: count}
    time_buckets: dict[int, dict[str, int]] = field(
        default_factory=lambda: {i: {} for i in range(4)}
    )
    # Per-source weights: source → (success_count, total_count)
    source_counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    def update(
        self,
        action_verb: str,
        confidence: float,
        latency_ms: float,
        source: str,
        ts: float,
        success: bool,
        pain_day: bool = False,
    ) -> None:
        """Record one observation. Updates action_stats, time_buckets, source_counts."""
        # 1. Get or create ActionStats entry for action_verb
        if action_verb not in self.action_stats:
            self.action_stats[action_verb] = ActionStats()
        stats = self.action_stats[action_verb]

        # 2. Increment frequency
        stats.frequency += 1

        # 3. Update last-used timestamp
        stats.last_used_ts = ts

        # 4. Update running mean for confidence + D7 Welford online variance
        old_mean = stats.mean_confidence
        stats._conf_sum += confidence
        stats.mean_confidence = stats._conf_sum / stats.frequency
        # Welford: M2 += (x - old_mean) * (x - new_mean)
        stats._conf_m2 += (confidence - old_mean) * (confidence - stats.mean_confidence)

        # D7: split into good-day / pain-day baselines
        if pain_day:
            stats.pain_day_conf_sum += confidence
            stats.pain_day_conf_count += 1
        else:
            stats.good_day_conf_sum += confidence
            stats.good_day_conf_count += 1

        # 5. Update running mean for latency
        stats._lat_sum += latency_ms
        stats.mean_latency_ms = stats._lat_sum / stats.frequency

        # 6. Update time_buckets: increment count for this action in the appropriate bucket
        bucket = PreferenceModel.time_bucket(ts)
        bucket_dict = self.time_buckets[bucket]
        bucket_dict[action_verb] = bucket_dict.get(action_verb, 0) + 1

        # 7. Update source_counts: (success_count, total_count)
        success_count, total_count = self.source_counts.get(source, (0, 0))
        total_count += 1
        if success:
            success_count += 1
        self.source_counts[source] = (success_count, total_count)

    def top_actions(self, time_bucket: int, n: int = 5) -> list[str]:
        """Return the top-n action verbs for the given time-of-day bucket."""
        bucket_dict = self.time_buckets.get(time_bucket, {})
        sorted_actions = sorted(bucket_dict, key=lambda verb: bucket_dict[verb], reverse=True)
        return sorted_actions[:n]

    def source_weights(self) -> dict[str, float]:
        """Return per-source success ratio (success_count / total_count) for all sources."""
        result: dict[str, float] = {}
        for source, (success_count, total_count) in self.source_counts.items():
            weight = success_count / total_count if total_count > 0 else 0.0
            result[source] = max(0.0, min(1.0, weight))
        return result

    @staticmethod
    def time_bucket(ts: float) -> int:
        """Map a Unix timestamp to a 6-hour bucket index (0–3)."""
        import datetime
        hour = datetime.datetime.fromtimestamp(ts).hour
        return hour // 6

    def to_json(self) -> str:
        """Serialize this PreferenceModel to a JSON string for AgentDB persistence."""
        action_stats_dict = {
            verb: {
                "frequency": stats.frequency,
                "mean_confidence": stats.mean_confidence,
                "mean_latency_ms": stats.mean_latency_ms,
                "last_used_ts": stats.last_used_ts,
                "_conf_sum": stats._conf_sum,
                "_lat_sum": stats._lat_sum,
                # D7: variance and day-split fields
                "_conf_m2": stats._conf_m2,
                "good_day_conf_sum": stats.good_day_conf_sum,
                "good_day_conf_count": stats.good_day_conf_count,
                "pain_day_conf_sum": stats.pain_day_conf_sum,
                "pain_day_conf_count": stats.pain_day_conf_count,
            }
            for verb, stats in self.action_stats.items()
        }
        # JSON requires string keys; convert int bucket indices to str
        time_buckets_dict = {
            str(bucket): counts
            for bucket, counts in self.time_buckets.items()
        }
        # Tuples serialize as lists in JSON; that's fine — from_json converts back
        source_counts_dict = {
            source: list(counts)
            for source, counts in self.source_counts.items()
        }
        return json.dumps({
            "action_stats": action_stats_dict,
            "time_buckets": time_buckets_dict,
            "source_counts": source_counts_dict,
        })

    @classmethod
    def from_json(cls, data: str) -> "PreferenceModel":
        """Reconstruct a PreferenceModel from a JSON string produced by to_json()."""
        raw = json.loads(data)

        # Reconstruct action_stats: dict[str, ActionStats]
        action_stats: dict[str, ActionStats] = {}
        for verb, d in raw.get("action_stats", {}).items():
            stats = ActionStats(
                frequency=d["frequency"],
                mean_confidence=d["mean_confidence"],
                mean_latency_ms=d["mean_latency_ms"],
                last_used_ts=d["last_used_ts"],
                _conf_sum=d["_conf_sum"],
                _lat_sum=d["_lat_sum"],
                # D7: backward-compatible defaults for models serialised before this fix
                _conf_m2=d.get("_conf_m2", 0.0),
                good_day_conf_sum=d.get("good_day_conf_sum", 0.0),
                good_day_conf_count=d.get("good_day_conf_count", 0),
                pain_day_conf_sum=d.get("pain_day_conf_sum", 0.0),
                pain_day_conf_count=d.get("pain_day_conf_count", 0),
            )
            action_stats[verb] = stats

        # Reconstruct time_buckets: dict[int, dict[str, int]] — string keys → int
        time_buckets: dict[int, dict[str, int]] = {i: {} for i in range(4)}
        for str_bucket, counts in raw.get("time_buckets", {}).items():
            time_buckets[int(str_bucket)] = dict(counts)

        # Reconstruct source_counts: dict[str, tuple[int, int]] — lists → tuples
        source_counts: dict[str, tuple[int, int]] = {
            source: (int(v[0]), int(v[1]))
            for source, v in raw.get("source_counts", {}).items()
        }

        model = cls.__new__(cls)
        model.action_stats = action_stats
        model.time_buckets = time_buckets
        model.source_counts = source_counts
        return model


# ---------------------------------------------------------------------------
# BehavioralTwinState — core class (Requirement 1.x, 2.x, 3.x, 4.x, 5.x, 6.x)
# ---------------------------------------------------------------------------

class BehavioralTwinState:
    """Persistent behavioral model for Brad.

    Lifecycle:
        twin = BehavioralTwinState(agent_db=db)
        await twin.start()          # concurrent AgentDB + ChromaDB init
        # ... pipeline runs ...
        await twin.stop()           # flush tasks, close connections

    Thread safety: all public methods are coroutines safe to call from
    the single asyncio event loop. TwinSnapshot is frozen — safe for
    concurrent reads without locks.
    """

    # ── Class constants ────────────────────────────────────────────────
    WORKING_SET_SIZE = 500
    SESSION_HISTORY_MAX = 20
    SESSION_HISTORY_PAIN_DAY = 10
    PAIN_DAY_ACTIVATE_THRESHOLD = 0.6
    PAIN_DAY_DEACTIVATE_THRESHOLD = 0.4
    PAIN_DAY_MIN_COMMANDS = 5
    PAIN_DAY_RECOMPUTE_INTERVAL_S = 60.0
    STARTUP_TIMEOUT_S = 5.0

    def __init__(
        self,
        agent_db: "AgentDB",
        chroma_dir: str = "./chroma_db",
        encoder=None,
    ) -> None:
        # ── Backing stores ─────────────────────────────────────────────
        self._agent_db = agent_db
        self._semantic_memory = SemanticMemory(
            chroma_dir=chroma_dir,
            agent_db=agent_db,
            encoder=encoder,
        )
        self._preference_model = PreferenceModel()

        # ── Session state ──────────────────────────────────────────────
        # Namespaced: "accessibility" is persistent; "dev_agent" is ephemeral
        # (clears itself after get_session_context("dev_agent") is called).
        self._session_history: dict[str, list[str]] = {
            "accessibility": [],
            "dev_agent": [],
        }
        self._working_set: list[dict] = []             # command dicts from AgentDB

        # ── Snapshot cache ─────────────────────────────────────────────
        self._current_snapshot: TwinSnapshot = _DEFAULT_SNAPSHOT

        # ── Lifecycle flags ────────────────────────────────────────────
        self._is_ready: bool = False

        # ── Pain day state ─────────────────────────────────────────────
        self._pain_day_active: bool = False
        self._pain_day_score: float = 0.0
        self._pain_day_task: Optional[asyncio.Task] = None
        self._last_pain_day_recompute: float = 0.0

        # ── Background task tracking ───────────────────────────────────
        self._pending_tasks: set[asyncio.Task] = set()

        # ── Per-session signal accumulators ────────────────────────────
        self._session_cmd_count: int = 0        # commands in current session
        self._session_fail_count: int = 0       # failed commands in current session
        self._session_clarify_count: int = 0    # CLARIFY responses in current session
        self._session_gesture_confs: list[float] = []  # gesture confidence values in session
        self._session_start_ts: float = time.monotonic()  # session start time
        # Last computed pain-day signals 3 & 4, stashed by _recompute_pain_day_score
        # so _pain_day_loop can log the real values into twin_pain_day_log
        # (previously hard-coded to 0.0).
        self._last_gesture_conf_delta: float = 0.0
        self._last_cmd_rate_delta: float = 0.0
        self._acoustic_profiler = None   # set via set_acoustic_profiler()
        # R-1: a bounded fatigue contribution from the FatigueMonitor observer.
        # It nudges the computed pain-day score but never owns activation (the
        # hysteresis below does). Stale values are ignored so a dead monitor
        # can't pin the score; zero when no fatigue (eval baselines unchanged).
        self._fatigue_signal: float = 0.0
        self._fatigue_signal_ts: float = 0.0
        self._manual_pain_day: bool = False  # user override via "hey agent pain day on"
        self._resource_governor = None   # set via set_resource_governor()
        self._gesture = None             # set via set_gesture_processor()
        self._bg_tasks: set = set()      # keeps fire-and-forget create_task refs alive

        # ── flare_profile degrade flags (loaded from AgentDB in start) ─────
        # Default True so behavior is unchanged until a profile is configured.
        self._flare_voice_degrades: bool = True
        self._flare_gesture_degrades: bool = True
        self._flare_tilt_degrades: bool = True
        self._flare_sound_degrades: bool = True

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Concurrent AgentDB + ChromaDB init via asyncio.gather.
        Sets is_ready=True on completion. Completes within STARTUP_TIMEOUT_S.
        """
        self._is_ready = False
        try:
            async with asyncio.timeout(self.STARTUP_TIMEOUT_S):
                await asyncio.gather(
                    self._init_agent_db(),
                    self._init_chroma(),
                )
        except asyncio.TimeoutError:
            log.warning("BehavioralTwinState: startup timed out — ChromaDB may be slow")
            self._semantic_memory._available = False
        except Exception as exc:
            log.warning("BehavioralTwinState: startup error: %s", exc)
        finally:
            self._is_ready = True
            self._pain_day_task = asyncio.create_task(self._pain_day_loop())
            log.info("BehavioralTwinState ready (chroma=%s)", self._semantic_memory.available)

    async def stop(self) -> None:
        """Flush all pending background tasks, persist final state, close."""
        # Cancel pain day recompute loop
        if self._pain_day_task:
            self._pain_day_task.cancel()
            try:
                await self._pain_day_task
            except asyncio.CancelledError:
                pass

        # Flush all pending observe() tasks
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        # Persist final state
        await self._persist_preference_model()
        await self._write_session_history()

        # Release ChromaDB file handles (prevents WinError 32 on Windows shutdown)
        await self._semantic_memory.stop()

        log.info("BehavioralTwinState stopped (%d tasks flushed)", len(self._pending_tasks))

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    # ── Public pipeline API ────────────────────────────────────────────

    async def get_snapshot(self) -> TwinSnapshot:
        """Return current behavioral state. Always returns; never raises.
        Returns _DEFAULT_SNAPSHOT if not ready or on error.
        Completes in < 50 ms under normal conditions.
        """
        try:
            if not self._is_ready:
                return _DEFAULT_SNAPSHOT
            return self._current_snapshot  # pre-built frozen dataclass
        except Exception as exc:
            log.warning("BehavioralTwinState.get_snapshot error: %s", exc)
            return _DEFAULT_SNAPSHOT

    async def observe(
        self,
        cmd: "Command",
        action_str: str,
        namespace: str = "accessibility",
    ) -> None:
        """Record a successful command. Non-blocking — schedules background task.
        Never raises.
        """
        try:
            task = asyncio.create_task(
                self._persist_observation(cmd, action_str, namespace=namespace),
                name=f"twin_observe_{id(cmd)}",
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except Exception as exc:
            log.warning("BehavioralTwinState.observe scheduling failed: %s", exc)

    async def record_failure(
        self,
        cmd: "Command",
        action_str: str,
        namespace: str = "accessibility",
    ) -> None:
        """Record a *failed* command so it feeds the pain-day fail signal.

        Deliberately the inverse of observe(): it moves the pain-day fail
        counters ONLY. It must never touch PreferenceModel, SemanticMemory, or
        the few-shot store — those are success-biased learning surfaces and a
        failed (often garbled) command must not be offered as a future example.
        Counters move only for the accessibility namespace, keeping the fail
        ratio consistent with observe()'s success counting (also
        accessibility-only); dev-agent failures are ignored here.

        signal_1 = fail / (success + fail), so a failure bumps BOTH the fail
        count and the command count to keep the denominator well-formed.
        Non-blocking, never raises.
        """
        try:
            if namespace != "accessibility":
                return
            self._session_cmd_count += 1
            self._session_fail_count += 1
            # Refresh the cached snapshot so the next route() sees the updated
            # command_count immediately; the pain-day score itself is recomputed
            # on the 60 s loop with hysteresis.
            if self._is_ready:
                self._current_snapshot = self._build_snapshot()
        except Exception as exc:
            log.warning("BehavioralTwinState.record_failure failed: %s", exc)

    async def query_similar(self, text: str, n: int = 5) -> list[dict]:
        """Return n most semantically similar past commands. Never raises."""
        try:
            return await self._semantic_memory.query_similar(text, n)
        except Exception as exc:
            log.warning("BehavioralTwinState.query_similar error: %s", exc)
            return []

    def get_session_context(self, namespace: str = "accessibility") -> list[str]:
        """Return SessionHistory for the given namespace as list[str].

        The "dev_agent" namespace clears itself after being read — it is
        ephemeral and must not pollute future accessibility context.
        """
        history = list(self._session_history.get(namespace, []))
        if namespace == "dev_agent":
            self._session_history["dev_agent"] = []
        return history

    def clear_dev_namespace(self) -> None:
        """Discard ephemeral dev-agent session history. Called after dev session ends."""
        self._session_history["dev_agent"] = []

    def set_acoustic_profiler(self, profiler) -> None:
        """Wire AcousticProfiler so voice clarity feeds into pain detection."""
        self._acoustic_profiler = profiler

    def set_resource_governor(self, governor) -> None:
        """Wire ResourceGovernor for sub-100ms flare notification.

        When set, set_manual_pain_day() calls governor.notify_pain_day_change()
        immediately instead of waiting up to POLL_INTERVAL_S (5s) for the next
        poll cycle.  Critical for sudden flares where VRAM must be freed fast.
        """
        self._resource_governor = governor

    def set_gesture_processor(self, gesture) -> None:
        """Wire GestureProcessor for immediate pain-day velocity-floor flips.

        Without this, the x0.70 gesture floor only changes on the next
        ContinuousTrainer tick (~60s after a flare is detected).
        """
        self._gesture = gesture

    def _on_pain_day_transition(self, active: bool) -> None:
        """Fire the immediate-response consumers on any pain-day state change.

        Shared by the manual override and the auto-detection loop so both
        paths converge to the same side effects with no poll/tick latency:
        - ResourceGovernor (flare fast-path, <100ms VRAM release)
        - GestureProcessor velocity floor (gated by flare_gesture_degrades)
        """
        if self._resource_governor is not None:
            self._resource_governor.notify_pain_day_change(1.0 if active else 0.0)
        if self._gesture is not None:
            try:
                self._gesture.apply_pain_day(active and self._flare_gesture_degrades)
            except Exception as exc:
                log.warning("BehavioralTwinState: gesture.apply_pain_day failed: %s", exc)

    # Bounded fatigue contribution: at most this much is added to the computed
    # pain-day score, and only while the signal is fresh. Small by design — the
    # observer advises, the hysteresis decides.
    _FATIGUE_NUDGE_WEIGHT: float = 0.10
    _FATIGUE_SIGNAL_TTL_S: float = 300.0

    def record_fatigue_signal(self, value: float) -> None:
        """R-1: the FatigueMonitor observer contributes a fatigue level in [0,1].

        This only nudges the computed pain-day score (see _recompute_pain_day_score)
        — PainDayEngine's hysteresis still owns activation. Call with 0.0 when
        fatigue subsides; stale values (> _FATIGUE_SIGNAL_TTL_S) are ignored.
        """
        self._fatigue_signal = max(0.0, min(1.0, float(value)))
        self._fatigue_signal_ts = time.monotonic()

    def _fresh_fatigue_signal(self) -> float:
        """Fatigue signal if recent enough, else 0.0 (dead-monitor guard).

        Reads defensively (getattr) so the score math is safe even for instances
        built via __new__ that skip __init__ (e.g. the pain-day property tests)."""
        signal = getattr(self, "_fatigue_signal", 0.0)
        if signal <= 0.0:
            return 0.0
        ts = getattr(self, "_fatigue_signal_ts", 0.0)
        if (time.monotonic() - ts) > self._FATIGUE_SIGNAL_TTL_S:
            return 0.0
        return signal

    def set_manual_pain_day(self, active: bool) -> None:
        """User override — immediately force pain_day_active state."""
        self._manual_pain_day = active
        self._pain_day_active = active
        # Rebuild the cached snapshot so HybridCoordinator.route() sees the new
        # state on the very next command (the loop only rebuilds every 60s).
        if self._is_ready:
            self._current_snapshot = self._build_snapshot()
        log.info("BehavioralTwinState: manual pain day %s", "ON" if active else "OFF")
        if self._agent_db and self._agent_db.available:
            t = asyncio.create_task(self._agent_db.set_manual_pain_day(active))
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)
        self._on_pain_day_transition(active)

    # ── Internal ───────────────────────────────────────────────────────

    async def _persist_preference_model(self) -> None:
        """Persist the current PreferenceModel to AgentDB settings_versions."""
        try:
            json_data = self._preference_model.to_json()
            await self._agent_db.log_settings_change(
                component="preference_model",
                key="snapshot",
                old_value=None,
                new_value=json_data,
                changed_by="BehavioralTwinState",
            )
        except Exception as exc:
            log.warning("BehavioralTwinState._persist_preference_model failed: %s", exc)

    async def _write_session_history(self) -> None:
        """Write accessibility session history to AgentDB twin_session_history table."""
        try:
            accessibility_history = self._session_history.get("accessibility", [])
            if not accessibility_history:
                return
            session_id = await self._agent_db.get_most_recent_session_id()
            if session_id is None:
                return
            history = [
                {"ts": time.time(), "cmd_text": text, "action": "", "source": ""}
                for text in accessibility_history
            ]
            await self._agent_db.write_session_history(session_id, history)
        except Exception as exc:
            log.warning("BehavioralTwinState._write_session_history failed: %s", exc)

    async def _persist_observation(
        self,
        cmd: "Command",
        action_str: str,
        namespace: str = "accessibility",
    ) -> None:
        """Background task: update SessionHistory and (for accessibility) PreferenceModel
        and SemanticMemory.

        Dev-agent observations are written only to the ephemeral "dev_agent"
        history slice; they never touch PreferenceModel, SemanticMemory, or the
        session signal accumulators, preventing dev debugging sessions from
        contaminating the accessibility few-shot context.
        """
        try:
            source = getattr(cmd, 'source', 'unknown')
            ts = time.time()
            text = getattr(cmd, 'text', '')

            # 1. Append to the correct namespace's session history
            max_history = (
                5 if namespace == "dev_agent"
                else (self.SESSION_HISTORY_PAIN_DAY if self._pain_day_active else self.SESSION_HISTORY_MAX)
            )
            hist = self._session_history[namespace]
            hist.append(text)
            if len(hist) > max_history:
                self._session_history[namespace] = hist[-max_history:]

            # 2. Accessibility-only: PreferenceModel, accumulators, SemanticMemory
            if namespace == "accessibility":
                action_verb = action_str.split()[0].upper() if action_str else "UNKNOWN"
                confidence = getattr(cmd, 'whisper_logprob', 0.0)

                # Update PreferenceModel (D7: pass current pain_day state)
                self._preference_model.update(
                    action_verb=action_verb,
                    confidence=confidence,
                    latency_ms=0.0,
                    source=source,
                    ts=ts,
                    success=True,
                    pain_day=self._pain_day_active,
                )

                # Update session signal accumulators
                self._session_cmd_count += 1
                if action_str.upper() == "CLARIFY":
                    self._session_clarify_count += 1
                if source == "gesture" and getattr(cmd, 'gesture_confidence', 0) > 0:
                    self._session_gesture_confs.append(cmd.gesture_confidence)

                # Add to SemanticMemory
                await self._semantic_memory.add(
                    text=text,
                    action=action_str,
                    source=source,
                    ts=ts,
                    success=True,
                )

                # Persist preference model to AgentDB
                await self._persist_preference_model()

            # 3. Rebuild _current_snapshot (always — pain-day snapshot uses accessibility slice)
            self._current_snapshot = self._build_snapshot()

        except Exception as exc:
            log.warning("BehavioralTwinState._persist_observation failed: %s", exc)

    def _build_snapshot(self) -> TwinSnapshot:
        """Build a fresh TwinSnapshot from current state."""
        current_bucket = PreferenceModel.time_bucket(time.time())
        return TwinSnapshot(
            pain_day_active=self._pain_day_active,
            preferred_actions=self._preference_model.top_actions(current_bucket, n=5),
            source_weights=self._preference_model.source_weights(),
            session_context=list(self._session_history.get("accessibility", [])),
            command_count_session=self._session_cmd_count,
            pain_day_score=self._pain_day_score,
            snapshot_ts=time.monotonic(),
            flare_voice_degrades=self._flare_voice_degrades,
            flare_gesture_degrades=self._flare_gesture_degrades,
            flare_tilt_degrades=self._flare_tilt_degrades,
            flare_sound_degrades=self._flare_sound_degrades,
        )

    async def _load_working_set(self) -> None:
        """Load 500 most recent successful commands from AgentDB on startup."""
        try:
            rows = await self._agent_db.get_recent_successful_commands(limit=self.WORKING_SET_SIZE)
            self._working_set = rows
            log.debug("BehavioralTwinState: loaded %d commands into working set", len(rows))
        except Exception as exc:
            log.warning("BehavioralTwinState._load_working_set failed: %s", exc)
            self._working_set = []

    async def _load_preference_model(self) -> None:
        """Reconstruct PreferenceModel from AgentDB settings_versions.

        Handles the historic double-serialization bug where log_settings_change
        called json.dumps() on an already-serialized JSON string.  We detect
        this by checking whether json.loads() returns a str or dict.
        """
        try:
            json_data = await self._agent_db.get_preference_model_snapshot()
            if json_data:
                # Safety: un-nest any legacy double-serialized value
                parsed = json.loads(json_data) if isinstance(json_data, str) else json_data
                if isinstance(parsed, str):
                    # Double-serialized row — parse one more time
                    parsed = json.loads(parsed)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Unexpected preference model type: {type(parsed)}")
                self._preference_model = PreferenceModel.from_json(json.dumps(parsed))
                log.debug("BehavioralTwinState: loaded PreferenceModel from AgentDB")
            else:
                log.debug("BehavioralTwinState: no saved PreferenceModel — using default")
        except Exception as exc:
            log.warning("BehavioralTwinState._load_preference_model failed: %s", exc)
            self._preference_model = PreferenceModel()

    async def _load_session_history(self) -> None:
        """Populate SessionHistory from most recent prior session in AgentDB.
        
        Loads from the most recent prior session, including abnormally terminated
        sessions with no ended_at timestamp (Requirement 3.6).
        """
        try:
            # Get the most recent prior session (not the current one)
            # We don't have a current session_id here, so get the most recent
            prior_session_id = await self._agent_db.get_most_recent_session_id()
            if prior_session_id is None:
                log.debug("BehavioralTwinState: no prior session found")
                return
            
            rows = await self._agent_db.read_session_history(
                session_id=prior_session_id,
                limit=self.SESSION_HISTORY_MAX,
            )
            
            if rows:
                self._session_history["accessibility"] = [row["cmd_text"] for row in rows]
                log.debug(
                    "BehavioralTwinState: loaded %d commands from prior session %d",
                    len(self._session_history["accessibility"]),
                    prior_session_id,
                )
            else:
                log.debug("BehavioralTwinState: no session history in prior session %d", prior_session_id)
        except Exception as exc:
            log.warning("BehavioralTwinState._load_session_history failed: %s", exc)
            self._session_history["accessibility"] = []

    def _recompute_pain_day_score(self) -> float:
        """Compute pain_day_score from current session signals. Returns [0.0, 1.0]."""
        total = self._session_cmd_count
        if total == 0:
            return 0.0

        # Signal 1: fail ratio
        signal_1 = self._session_fail_count / total

        # Signal 2: clarify ratio
        signal_2 = self._session_clarify_count / total

        # Signal 3: gesture confidence drop vs good-day baseline (D7).
        # Uses the per-action good_day_mean_confidence from PreferenceModel
        # rather than the pooled working-set mean, which included pain-day
        # observations that were artificially lowering the baseline over time.
        gesture_stats = [
            s for s in self._preference_model.action_stats.values()
            if s.good_day_conf_count > 0
        ]
        good_day_baseline = (
            sum(s.good_day_mean_confidence for s in gesture_stats) / len(gesture_stats)
            if gesture_stats else 0.0
        )
        if good_day_baseline > 0 and self._session_gesture_confs:
            current_conf = sum(self._session_gesture_confs) / len(self._session_gesture_confs)
            signal_3 = max(0.0, (good_day_baseline - current_conf) / good_day_baseline)
        else:
            signal_3 = 0.0

        # Signal 4: command rate drop (vs baseline estimated from working set)
        elapsed_hours = (time.monotonic() - self._session_start_ts) / 3600.0
        if elapsed_hours > 0 and self._working_set:
            # Heuristic: working set represents ~8 hours of typical usage
            baseline_rate = len(self._working_set) / 8.0  # commands per hour
            current_rate = total / elapsed_hours
            signal_4 = max(0.0, (baseline_rate - current_rate) / baseline_rate) if baseline_rate > 0 else 0.0
        else:
            signal_4 = 0.0

        # Stash signals 3 & 4 so _pain_day_loop logs the real values into
        # twin_pain_day_log (computed here regardless of the manual-override
        # early return below).
        self._last_gesture_conf_delta = signal_3
        self._last_cmd_rate_delta = signal_4

        # Signal 5: voice clarity drop (from AcousticProfiler)
        # Captures voice softening during RA flares — the most distinctive
        # early indicator for users like Brad.
        if self._acoustic_profiler is not None:
            signal_5 = self._acoustic_profiler.voice_clarity_score()
        else:
            signal_5 = 0.0

        # If manual override is active, drive score to 1.0 regardless
        if self._manual_pain_day:
            return 1.0

        # Reweight: voice clarity gets 0.15, others compress proportionally
        score = (
            0.30 * signal_1 +   # fail ratio
            0.20 * signal_2 +   # clarify ratio
            0.15 * signal_3 +   # gesture confidence drop
            0.20 * signal_4 +   # command rate drop
            0.15 * signal_5     # voice clarity drop  ← new
        )
        # R-1: bounded additive fatigue nudge from the FatigueMonitor observer.
        # Zero when no (fresh) fatigue signal, so the historical 5-signal score is
        # unchanged and eval baselines hold; otherwise a small push toward the
        # activation threshold that the hysteresis below still owns.
        score += self._FATIGUE_NUDGE_WEIGHT * self._fresh_fatigue_signal()
        return max(0.0, min(1.0, score))

    async def _pain_day_loop(self) -> None:
        """Background task: recompute pain_day_score every 60 seconds.

        Applies hysteresis:
        - Activate when score > 0.6 AND session_cmd_count >= PAIN_DAY_MIN_COMMANDS
        - Deactivate when score < 0.4
        """
        while True:
            try:
                await asyncio.sleep(self.PAIN_DAY_RECOMPUTE_INTERVAL_S)

                score = self._recompute_pain_day_score()
                self._pain_day_score = score

                old_active = self._pain_day_active

                # Hysteresis state machine
                if not self._pain_day_active:
                    if score > self.PAIN_DAY_ACTIVATE_THRESHOLD and self._session_cmd_count >= self.PAIN_DAY_MIN_COMMANDS:
                        self._pain_day_active = True
                        log.info("BehavioralTwinState: PainDayMode ACTIVATED (score=%.3f)", score)
                else:
                    if score < self.PAIN_DAY_DEACTIVATE_THRESHOLD:
                        self._pain_day_active = False
                        log.info("BehavioralTwinState: PainDayMode DEACTIVATED (score=%.3f)", score)

                # Rebuild snapshot if state changed
                if old_active != self._pain_day_active:
                    self._current_snapshot = self._build_snapshot()
                    # Fire immediate consumers (governor + gesture) so an
                    # auto-detected flare doesn't wait for the next poll/tick.
                    self._on_pain_day_transition(self._pain_day_active)

                # Log pain day score to AgentDB
                try:
                    session_id = await self._agent_db.get_most_recent_session_id()
                    if session_id is not None:
                        await self._agent_db.log_pain_day(
                            session_id=session_id,
                            score=score,
                            active=self._pain_day_active,
                            fail_ratio=self._session_fail_count / max(1, self._session_cmd_count),
                            clarify_ratio=self._session_clarify_count / max(1, self._session_cmd_count),
                            gesture_conf_delta=self._last_gesture_conf_delta,
                            cmd_rate_delta=self._last_cmd_rate_delta,
                        )
                except Exception as exc:
                    log.warning("BehavioralTwinState._pain_day_loop: log_pain_day failed: %s", exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("BehavioralTwinState._pain_day_loop error: %s", exc)

    async def _init_agent_db(self) -> None:
        """Load working set, preferences, and session history from AgentDB."""
        await self._load_working_set()
        await self._load_preference_model()
        await self._load_session_history()
        await self._load_flare_profile()

    async def _load_flare_profile(self) -> None:
        """Load which sensors degrade on a flare from AgentDB.flare_profile.

        Leaves the all-True defaults in place when no profile row exists, so
        pain-day relaxation is unchanged until Brad configures it on the iPad.
        """
        try:
            profile = await self._agent_db.get_flare_profile()
            if profile:
                self._flare_voice_degrades = bool(profile.get("voice_degrades", True))
                self._flare_gesture_degrades = bool(profile.get("gesture_degrades", True))
                self._flare_tilt_degrades = bool(profile.get("tilt_degrades", True))
                self._flare_sound_degrades = bool(profile.get("sound_degrades", True))
                log.info(
                    "BehavioralTwinState: flare degrade flags — voice=%s gesture=%s "
                    "tilt=%s sound=%s",
                    self._flare_voice_degrades, self._flare_gesture_degrades,
                    self._flare_tilt_degrades, self._flare_sound_degrades,
                )
        except Exception as exc:
            log.warning("BehavioralTwinState._load_flare_profile failed: %s", exc)

    def set_flare_profile(self, flags: dict) -> None:
        """Apply updated flare degrade flags live (from the iPad FlareProfileSheet).

        Updates only the keys present in `flags` and rebuilds the cached
        snapshot so HybridCoordinator.route() honours them on the next command
        without a restart. Keys: voice_degrades / gesture_degrades /
        tilt_degrades / sound_degrades.
        """
        if "voice_degrades" in flags:
            self._flare_voice_degrades = bool(flags["voice_degrades"])
        if "gesture_degrades" in flags:
            self._flare_gesture_degrades = bool(flags["gesture_degrades"])
        if "tilt_degrades" in flags:
            self._flare_tilt_degrades = bool(flags["tilt_degrades"])
        if "sound_degrades" in flags:
            self._flare_sound_degrades = bool(flags["sound_degrades"])
        if self._is_ready:
            self._current_snapshot = self._build_snapshot()
        log.info(
            "BehavioralTwinState: flare profile updated — voice=%s gesture=%s "
            "tilt=%s sound=%s",
            self._flare_voice_degrades, self._flare_gesture_degrades,
            self._flare_tilt_degrades, self._flare_sound_degrades,
        )

    async def _init_chroma(self) -> None:
        """Connect ChromaDB, create/open collection."""
        try:
            async with asyncio.timeout(self.STARTUP_TIMEOUT_S):
                available = await self._semantic_memory.start()
                if not available:
                    log.warning("SemanticMemory: ChromaDB unavailable — AgentDB fallback active")
        except asyncio.TimeoutError:
            log.warning("SemanticMemory: ChromaDB init timed out — AgentDB fallback active")
            self._semantic_memory._available = False
        except Exception as exc:
            log.warning("SemanticMemory: ChromaDB init error: %s — AgentDB fallback active", exc)
            self._semantic_memory._available = False
