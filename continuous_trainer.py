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

if TYPE_CHECKING:
    from command_executor import Command
    from db import AgentDB
    from hybrid_coordinator import CoordinatorConfig


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
    ) -> None:
        self._db = agent_db
        self._interval = adaptation_interval_s
        self._hotword_threshold = hotword_threshold
        self._gesture_min = gesture_samples_min
        self._cloud_limit = cloud_escalation_limit
        self._failure_limit = local_failure_limit
        self._gate1_step = gate1_relaxation_step
        self._config = config

        self._running = False
        self._task: Optional[asyncio.Task] = None

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

    async def record_success(
        self,
        cmd: "Command",
        action_str: str,
        domain: str = "command",
        command_id: Optional[int] = None,
    ) -> None:
        """Record a successfully executed command as a few-shot example."""
        await self._db.upsert_few_shot_example(cmd, action_str, domain, command_id)

        # Gesture confidence tracking
        if cmd.source == "gesture":
            gesture = cmd.params.get("gesture", "UNKNOWN")
            lidar_depth = cmd.params.get("lidar_depth_m")
            await self._db.record_gesture_sample(
                gesture, cmd.gesture_confidence,
                lidar_depth_m=lidar_depth,
                command_id=command_id,
            )

    async def get_few_shot_examples(
        self,
        cmd: "Command",
        n: int = 5,
        domain: str = "command",
    ) -> list[dict]:
        """Return the n best few-shot examples for this command."""
        return await self._db.get_few_shot_examples(cmd, n=n, domain=domain)

    async def get_hotwords(self) -> list[str]:
        return await self._db.get_hotwords()

    def record_gesture_sample(self, gesture: str, confidence: float) -> None:
        """Synchronous wrapper for direct calls from GestureProcessor."""
        asyncio.ensure_future(
            self._db.record_gesture_sample(gesture, confidence)
        )

    async def get_gesture_floor(self, gesture: str) -> float:
        return await self._db.get_gesture_floor(gesture)

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
        """Record a user correction locally and forward to AgentCore."""
        # Store the correct mapping as a few-shot example
        await self._db.upsert_few_shot_example(cmd, correct_action, "command", command_id)
        if command_id and command_id > 0:
            await self._db.mark_command_corrected(command_id, correct_action)
        log.info("Correction stored: %r → %s (was %s)", cmd.text, correct_action, wrong_action)

        # Forward to AgentCore (fire-and-forget)
        asyncio.create_task(
            self._send_correction_to_agentcore(cmd.text, wrong_action, correct_action)
        )

    async def _send_correction_to_agentcore(
        self, original_text: str, wrong_action: str, correct_action: str
    ) -> None:
        try:
            from agentcore_fallback.client import AgentCoreFallbackClient
            client = AgentCoreFallbackClient()
            result = await client.record_correction(
                original_text=original_text,
                wrong_action=wrong_action,
                correct_action=correct_action,
            )
            log.info("Correction sent to AgentCore: %s", result)
        except ImportError:
            log.debug("AgentCore not available — correction stored locally only")
        except Exception as exc:
            log.warning("Failed to send correction to AgentCore: %s", exc)

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
        entries = await self._db.get_recent_routing_stats(limit=1000)
        if entries:
            self._adapt_gate1_threshold(entries)
        await self._db.promote_hotwords(self._hotword_threshold)
        await self._update_gesture_calibration()

    def _adapt_gate1_threshold(self, entries: list[dict]) -> None:
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

        if cloud_rate > self._cloud_limit and failure_rate < self._failure_limit:
            old = self._config.whisper_logprob_min
            self._config.whisper_logprob_min = min(
                -0.1, old + self._gate1_step
            )
            log.info(
                "Gate 1 threshold relaxed: %.2f → %.2f "
                "(cloud_rate=%.0f%% failure_rate=%.0f%%)",
                old, self._config.whisper_logprob_min,
                cloud_rate * 100, failure_rate * 100,
            )

    async def _update_gesture_calibration(self) -> None:
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
            old_floor = await self._db.get_gesture_floor(gesture)
            await self._db.update_gesture_calibration(
                gesture, floor, len(samples), p10
            )
            if abs(floor - old_floor) > 0.001:
                log.info(
                    "Gesture %s confidence floor: %.3f → %.3f (%d samples)",
                    gesture, old_floor, floor, len(samples),
                )
