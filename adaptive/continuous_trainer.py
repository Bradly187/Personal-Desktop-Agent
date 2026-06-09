"""ContinuousTrainer — background learning system for the Personal Desktop Agent.

Adapts routing thresholds, maintains few-shot examples, tracks Whisper hotwords,
and calibrates per-gesture confidence floors. All persistence is delegated to
AgentDB (agent.db); this class holds only adaptation logic and in-flight state.

Requirements satisfied (Requirement 14):
  14.1 Record successful input→action pairs in few-shot DB
  14.2 Rank stored examples by token overlap weighted by recency + usage count
  14.3 Relax Gate 1 confidence threshold when cloud escalation > 30% and
       local failure rate < 10%
  14.4 Add words to Whisper hotwords list when they appear ≥3× in successes
  14.5 Set gesture confidence floor to p10(observed) - 0.05 when ≥10 samples
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

log = logging.getLogger(__name__)

from core.async_utils import fire_and_log

if TYPE_CHECKING:
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from core.command_executor import Command
    from storage.db import AgentDB
    from core.hybrid_coordinator import CoordinatorConfig


class ContinuousTrainer:
    """Background learning system.

    Wire-up:
        trainer = ContinuousTrainer(agent_db, config=cfg)
        await trainer.start()

        # After a successful execution:
        await trainer.record_success(cmd, action_str, command_id=command_id)

        # On Ctrl-C:
        await trainer.stop()
    """

    def __init__(
        self,
        agent_db: "AgentDB",
        adaptation_interval_s: float = 300.0,
        hotword_threshold: int = 3,
        gesture_samples_min: int = 10,
        cloud_escalation_limit: float = 0.30,
        local_failure_limit: float = 0.10,
        gate1_relaxation_step: float = 0.05,
        config: Optional["CoordinatorConfig"] = None,
        twin_state: Optional["BehavioralTwinState"] = None,
        gesture_processor=None,    # GestureProcessor — receives calibrated thresholds
    ) -> None:
        self._db = agent_db
        self._interval = adaptation_interval_s
        self._hotword_threshold = hotword_threshold
        self._gesture_min = gesture_samples_min
        self._cloud_limit = cloud_escalation_limit
        self._failure_limit = local_failure_limit
        self._gate1_step = gate1_relaxation_step
        self._gesture_proc = gesture_processor
        self._config = config
        self._twin = twin_state

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._memory = None   # MemoryManager — wired via set_memory()
        self.slo_status: dict[str, str] = {}   # gap H: latest per-domain SLO verdicts
        # D9: intra-session velocity calibration trigger
        self._velocity_sample_counter: int = 0
        self._velocity_intra_session_threshold: int = 20

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        if not self._db.available:
            log.warning("ContinuousTrainer: AgentDB unavailable — not starting.")
            return
        self._running = True
        self._task = asyncio.create_task(self._adaptation_loop())
        log.info("ContinuousTrainer started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Flush final gesture calibration for all gestures with enough samples
        await self._update_gesture_calibration()
        log.info("ContinuousTrainer stopped")

    # ---------------------------------------------------------------------- #
    # Public API — called by HybridCoordinator
    # ---------------------------------------------------------------------- #

    def set_memory(self, memory) -> None:
        """Wire MemoryManager for standardised storage access."""
        self._memory = memory

    async def record_success(
        self,
        cmd: "Command",
        action_str: str,
        domain: str = "command",
        command_id: Optional[int] = None,
        namespace: str = "accessibility",
    ) -> None:
        """Record a successfully executed command as a few-shot example."""
        # Phase C: route through MemoryManager when available
        if self._memory is not None:
            await self._memory.write_state(
                "few_shot_example",
                (cmd, action_str, domain, command_id),
                namespace=namespace,
            )
        else:
            await self._db.upsert_few_shot_example(cmd, action_str, domain, command_id)

        # Feed observation into twin state (non-blocking)
        if self._twin:
            await self._twin.observe(cmd, action_str, namespace=namespace)

        # Gesture confidence tracking
        if cmd.source == "gesture":
            gesture = cmd.params.get("gesture", "UNKNOWN")
            lidar_depth = cmd.params.get("lidar_depth_m")
            await self._db.record_gesture_sample(
                gesture, cmd.gesture_confidence,
                lidar_depth_m=lidar_depth,
                command_id=command_id,
            )

    async def record_failure(
        self,
        cmd: "Command",
        action_str: str,
        command_id: Optional[int] = None,
        namespace: str = "accessibility",
    ) -> None:
        """Record a *failed* command for the twin's pain-day fail signal only.

        The deliberately narrow counterpart to record_success: it forwards to
        the twin's record_failure (which moves pain-day fail counters only) and
        writes NOTHING to the few-shot store, SemanticMemory, or gesture
        samples. Failures must never become few-shot examples — those stores are
        success-biased. command_id is accepted for call-site symmetry.
        """
        if self._db and self._db.available:
            await self._db.upsert_few_shot_counterexample(
                cmd, action_str, "command", "pipeline_failure", command_id
            )
        if self._twin:
            await self._twin.record_failure(cmd, action_str, namespace=namespace)

    async def drain_and_persist_velocity(self, pain_day: bool = False) -> None:
        """Drain velocity samples from GestureProcessor and persist to DB.

        D3: Called for every gesture command that clears Gate 1, not only on
        full-pipeline success. This prevents velocity calibration from becoming
        biased toward gestures that happened to hit valid targets.

        D9: Triggers an intra-session velocity calibration pass after every
        _velocity_intra_session_threshold samples without waiting for the 300s
        timer.
        """
        if self._gesture_proc is None:
            return
        new_samples = 0
        for g_name, vel in self._gesture_proc.drain_velocity_samples():
            await self._db.record_gesture_velocity(g_name, vel, pain_day=pain_day)
            new_samples += 1

        if new_samples == 0:
            return

        # D9: trigger intra-session calibration when enough samples accumulate
        self._velocity_sample_counter += new_samples
        if self._velocity_sample_counter >= self._velocity_intra_session_threshold:
            self._velocity_sample_counter = 0
            await self._update_gesture_velocity_calibration(pain_day_active=pain_day)
            log.debug(
                "ContinuousTrainer: intra-session velocity calibration triggered "
                "(%d new samples)", new_samples,
            )

    async def get_few_shot_examples(
        self,
        cmd: "Command",
        n: int = 5,
        domain: str = "command",
    ) -> list[dict]:
        """Return the n best few-shot examples for this command."""
        return await self._db.get_few_shot_examples(cmd, n=n, domain=domain)

    async def get_few_shot_counterexamples(
        self,
        cmd: "Command",
        n: int = 3,
        domain: str = "command",
    ) -> list[dict]:
        """Return up to n counterexamples (wrong actions) for this command."""
        return await self._db.get_few_shot_counterexamples(cmd, n=n, domain=domain)

    async def get_hotwords(self) -> list[str]:
        return await self._db.get_hotwords()

    def record_gesture_sample(self, gesture: str, confidence: float) -> None:
        """Synchronous wrapper for direct calls from GestureProcessor."""
        fire_and_log(
            self._db.record_gesture_sample(gesture, confidence),
            log, "gesture_sample write",
        )

    async def get_gesture_floor(self, gesture: str) -> float:
        return await self._db.get_gesture_floor(gesture)

    async def load_velocity_calibration(self) -> None:
        """Load persisted velocity floors from DB into GestureProcessor at startup (Gap 3).

        Only gestures with an actual calibration row contribute: SWIPE floors
        (normalized coords/s) and PUSH floors (m/s) are different units, so an
        uncalibrated gesture must fall back to GestureProcessor's own per-class
        default rather than inheriting a number from the other class. On a
        fresh DB this is a no-op (built-in defaults stay in effect). Preserves
        the current pain-day relaxation instead of clobbering it to False.
        """
        if self._gesture_proc is None or not self._db.available:
            return
        motion_gestures = [
            "PEACE_SWIPE_LEFT", "PEACE_SWIPE_RIGHT", "PEACE_SWIPE_UP", "PEACE_SWIPE_DOWN",
            "OPEN_PUSH", "OPEN_PULL",
            "GRAB_SNAP_LEFT", "GRAB_SNAP_RIGHT", "GRAB_NEXT_MONITOR", "GRAB_PREV_MONITOR",
        ]
        calibrated: dict[str, float] = {}
        for gesture in motion_gestures:
            floor = await self._db.get_gesture_velocity_floor(gesture)
            if floor is None:               # no calibration row — keep class default
                continue
            key = "SWIPE" if "SWIPE" in gesture else "PUSH"
            if key not in calibrated or floor < calibrated[key]:
                calibrated[key] = floor
        if calibrated:
            pain_active = False
            if self._twin is not None:
                try:
                    pain_active = bool((await self._twin.get_snapshot()).pain_day_active)
                except Exception:
                    pain_active = False
            self._gesture_proc.set_velocity_thresholds(
                calibrated, pain_day_active=pain_active
            )
            log.info("GestureProcessor: loaded velocity floors from DB — %s", calibrated)

    # ---------------------------------------------------------------------- #
    # Correction feedback
    # ---------------------------------------------------------------------- #

    async def record_correction(
        self,
        cmd: "Command",
        wrong_action: str,
        correct_action: str,
        command_id: Optional[int] = None,
    ) -> None:
        """Record a user correction as a few-shot example."""
        await self._db.upsert_few_shot_counterexample(
            cmd, wrong_action, "command", "user_correction", command_id
        )
        await self._db.upsert_few_shot_example(cmd, correct_action, "command", command_id)
        if command_id and command_id > 0:
            await self._db.mark_command_corrected(command_id, correct_action)
        log.info("Correction stored: %r → %s (was %s)", cmd.text, correct_action, wrong_action)

    # ---------------------------------------------------------------------- #
    # Adaptation loop
    # ---------------------------------------------------------------------- #

    async def _adaptation_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                await self._adapt()
            except Exception as exc:
                log.error("ContinuousTrainer adaptation error: %s", exc)

    async def _adapt(self) -> None:
        log.debug("ContinuousTrainer: running adaptation pass")
        from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
        snapshot = _DEFAULT_SNAPSHOT
        if self._twin:
            try:
                snapshot = await self._twin.get_snapshot()
            except Exception as exc:
                log.warning("ContinuousTrainer: twin snapshot failed: %s", exc)
        entries = await self._db.get_recent_routing_stats(limit=1000)
        if entries:
            await self._adapt_gate1_threshold(entries, pain_day_active=snapshot.pain_day_active)
        await self._db.promote_hotwords(self._hotword_threshold)
        await self._update_gesture_calibration(pain_day_active=snapshot.pain_day_active)
        await self._update_gesture_velocity_calibration(pain_day_active=snapshot.pain_day_active)
        await self._adapt_per_domain_slo()

    async def _adapt_per_domain_slo(self) -> None:
        """Evaluate rolling per-domain inference stats against per-domain SLOs (gap H).

        Conservative by design: it LOGS breaches to adaptation_log (domain-tagged)
        and exposes them via `self.slo_status` for the operator/UI — it does not
        aggressively re-tune dev model selection (risky) or train a route
        classifier (data-blocked). The one concrete knob it sets is a per-domain
        Gate-4 latency override on the config for domains chronically breaching
        their latency SLO, so the gated path can escalate sooner for them.
        """
        if not self._config:
            return
        try:
            from core.slo import evaluate, BREACH_LATENCY, BREACH_SUCCESS, HEADROOM
            stats = await self._db.get_inference_stats_by_domain(limit=1000)
        except Exception as exc:
            log.debug("ContinuousTrainer._adapt_per_domain_slo: stats failed: %s", exc)
            return

        status: dict[str, str] = {}
        for domain, s in stats.items():
            if s.get("count", 0) < self._gesture_min:
                continue   # not enough data to judge this domain yet
            slo = self._config.slo.get(domain)
            verdict = evaluate(slo, s.get("p50_latency_ms"), s.get("success_rate"))
            status[domain] = verdict
            if verdict in (BREACH_LATENCY, BREACH_SUCCESS):
                log.info("SLO breach: domain=%s verdict=%s p50=%.0fms success=%.2f (budget=%.0fms)",
                         domain, verdict, s.get("p50_latency_ms") or 0,
                         s.get("success_rate") or 0, slo.latency_budget_ms)
                await self._db.log_adaptation(
                    component=f"slo:{domain}",
                    metric_before=slo.latency_budget_ms,
                    metric_after=s.get("p50_latency_ms") or slo.latency_budget_ms,
                    domain=domain,
                )
                if verdict == BREACH_LATENCY and domain != "command":
                    # Tighten this domain's Gate-4 budget toward observed p50 so the
                    # gated path (if it ever routes this domain) escalates sooner.
                    self._config.per_domain_latency_budget[domain] = slo.latency_budget_ms
        self.slo_status = status

    async def _adapt_gate1_threshold(self, entries: list[dict], pain_day_active: bool = False) -> None:
        """Requirement 14.3 — relax Gate 1 when cloud escalation is high."""
        if not self._config:
            return
        routed = [e for e in entries if e.get("route") in ("local", "cloud")]
        if len(routed) < 20:
            return
        cloud_count = sum(1 for e in routed if e.get("route") == "cloud")
        cloud_rate = cloud_count / len(routed)
        error_count = sum(
            1 for e in entries
            if (e.get("action") or "").startswith("CLARIFY")
        )
        failure_rate = error_count / len(entries) if entries else 0.0

        await self._adapt_gate1_with_log(cloud_rate, failure_rate, pain_day_active)

    async def _adapt_gate1_with_log(
        self,
        cloud_rate: float,
        failure_rate: float,
        pain_day_active: bool,
    ) -> None:
        """D5: Inner gate1 adaptation with effectiveness logging and rollback."""
        if not self._config:
            return

        # D5: check last 2 passes — if cloud_rate increased both times, roll back
        try:
            if self._db and self._db.available:
                history = await self._db.get_recent_adaptation_log("gate1", limit=2)
                if len(history) >= 2:
                    if (history[0]["cloud_rate"] > history[1]["cloud_rate"]
                            and history[1]["cloud_rate"] > (history[1].get("metric_before") or 0.0)):
                        log.warning(
                            "Gate 1: cloud_rate increased in 2 consecutive passes "
                            "(%.0f%% → %.0f%%) — rolling back last threshold change",
                            history[1]["cloud_rate"] * 100, history[0]["cloud_rate"] * 100,
                        )
                        if history[0].get("metric_before") is not None:
                            self._config.whisper_logprob_min = history[0]["metric_before"]
                        await self._db.mark_adaptation_rolled_back(history[0]["id"])
                        return
        except Exception as exc:
            log.debug("Gate 1 rollback check skipped: %s", exc)

        if cloud_rate > self._cloud_limit and failure_rate < self._failure_limit:
            if pain_day_active:
                log.info("Gate 1 threshold relaxation suppressed (pain day active)")
                return
            old = self._config.whisper_logprob_min
            self._config.whisper_logprob_min = max(-1.5, old - self._gate1_step)
            log.info(
                "Gate 1 threshold loosened: %.2f → %.2f "
                "(cloud_rate=%.0f%% failure_rate=%.0f%%)",
                old, self._config.whisper_logprob_min,
                cloud_rate * 100, failure_rate * 100,
            )
            try:
                if self._db and self._db.available:
                    await self._db.log_adaptation(
                        component="gate1",
                        metric_before=old,
                        metric_after=self._config.whisper_logprob_min,
                        cloud_rate=cloud_rate,
                        failure_rate=failure_rate,
                    )
                    await self._db.log_settings_change(
                        component="coordinator",
                        key="whisper_logprob_min",
                        old_value=old,
                        new_value=self._config.whisper_logprob_min,
                        changed_by="continuous_trainer",
                    )
            except Exception as exc:
                log.debug("Gate 1 adaptation log skipped: %s", exc)

    async def _update_gesture_calibration(self, pain_day_active: bool = False) -> None:
        """Requirement 14.5 — set gesture floor to p10(observed) - 0.05."""
        gestures = ["POINT", "PINCH", "OPEN_PALM", "FIST"]
        for gesture in gestures:
            samples = await self._db.get_recent_gesture_samples(gesture, limit=500)
            if len(samples) < self._gesture_min:
                continue
            samples_sorted = sorted(samples)
            p10_idx = max(0, int(len(samples_sorted) * 0.10) - 1)
            p10 = samples_sorted[p10_idx]
            floor = max(0.0, p10 - 0.05)
            if pain_day_active:
                floor = max(0.0, floor - 0.05)  # additional reduction on pain days
            old_floor = await self._db.get_gesture_floor(gesture)
            await self._db.update_gesture_calibration(
                gesture, floor, len(samples), p10
            )
            if abs(floor - old_floor) > 0.001:
                log.info(
                    "Gesture %s confidence floor: %.3f → %.3f (%d samples)",
                    gesture, old_floor, floor, len(samples),
                )

    async def _update_gesture_velocity_calibration(
        self, pain_day_active: bool = False
    ) -> None:
        """Adapt velocity thresholds from observed motion-gesture samples.

        Mirrors the confidence-floor pattern:
          velocity_floor = p10(observed_velocities) [- pain_day reduction]

        On pain days, all floors are multiplied by PAIN_DAY_VELOCITY_FACTOR
        (0.70) so slower, less-forceful gestures still register.

        After calibration, pushes the updated thresholds directly into the
        GestureProcessor instance so changes take effect immediately.
        """
        from sensors.gesture_processor import _PAIN_DAY_VELOCITY_FACTOR

        motion_gestures = [
            "PEACE_SWIPE_LEFT", "PEACE_SWIPE_RIGHT",
            "PEACE_SWIPE_UP",   "PEACE_SWIPE_DOWN",
            "OPEN_PUSH",  "OPEN_PULL",
            "GRAB_SNAP_LEFT", "GRAB_SNAP_RIGHT",
            "GRAB_NEXT_MONITOR", "GRAB_PREV_MONITOR",
        ]

        calibrated: dict[str, float] = {}
        for gesture in motion_gestures:
            samples = await self._db.get_recent_gesture_velocities(gesture, limit=500)
            if len(samples) < self._gesture_min:
                continue
            samples_sorted = sorted(samples)
            p10_idx = max(0, int(len(samples_sorted) * 0.10) - 1)
            p10 = samples_sorted[p10_idx]
            floor = max(0.1, p10)              # always at least 0.1 coords/s
            old_floor = await self._db.get_gesture_velocity_floor(gesture)
            if old_floor is None:              # first calibration for this gesture
                old_floor = floor
            await self._db.update_gesture_velocity_calibration(
                gesture, floor, len(samples), p10
            )
            # Group swipes and push/pull under canonical keys for GestureProcessor
            if "SWIPE" in gesture:
                calibrated["SWIPE"] = min(calibrated.get("SWIPE", floor), floor)
            elif "PUSH" in gesture or "PULL" in gesture or "SNAP" in gesture or "MONITOR" in gesture:
                calibrated["PUSH"] = min(calibrated.get("PUSH", floor), floor)
            if abs(floor - old_floor) > 0.05:
                log.info(
                    "Gesture %s velocity floor: %.2f → %.2f (%d samples)",
                    gesture, old_floor, floor, len(samples),
                )

        # Push calibrated thresholds into GestureProcessor (takes effect immediately)
        if calibrated and self._gesture_proc is not None:
            self._gesture_proc.set_velocity_thresholds(
                calibrated, pain_day_active=pain_day_active
            )
