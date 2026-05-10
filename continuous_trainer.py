"""ContinuousTrainer — background learning system for the Personal Desktop Agent.

Adapts routing thresholds, maintains a few-shot example SQLite database,
tracks Whisper hotwords, and calibrates per-gesture confidence floors.

Requirements satisfied (Requirement 14):
  14.1 Record successful input→action pairs in few-shot SQLite DB
  14.2 Rank stored examples by token overlap weighted by recency + usage count
  14.3 Relax Gate 1 confidence threshold when cloud escalation > 30% and
       local failure rate < 10%
  14.4 Add words to Whisper hotwords list when they appear ≥3× in successes
  14.5 Set gesture confidence floor to p10(observed) - 0.05 when ≥10 samples

Graceful degradation: if aiosqlite is unavailable the trainer disables itself
and logs a warning rather than crashing the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

log = logging.getLogger(__name__)

try:
    import aiosqlite
    _AIOSQLITE_AVAILABLE = True
except ImportError:
    _AIOSQLITE_AVAILABLE = False
    log.warning("aiosqlite not installed — ContinuousTrainer disabled. "
                "Install with: pip install aiosqlite")

if TYPE_CHECKING:
    from command_executor import Command
    from hybrid_coordinator import CoordinatorConfig


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS examples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    domain       TEXT    NOT NULL DEFAULT 'command',
    ts           REAL    NOT NULL,
    usage_count  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS word_counts (
    word   TEXT PRIMARY KEY,
    count  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hotwords (
    word  TEXT PRIMARY KEY,
    added REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_examples_ts ON examples (ts);
"""


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _recency_weight(ts: float, now: float, half_life_days: float = 30.0) -> float:
    age_days = (now - ts) / 86400.0
    return math.exp(-age_days * math.log(2) / half_life_days)


def _score(row: dict, query_tokens: set[str], now: float) -> float:
    overlap = _jaccard(query_tokens, _tokens(row["text"]))
    recency = _recency_weight(row["ts"], now)
    usage = math.log1p(row["usage_count"])
    return overlap * recency * usage


# ---------------------------------------------------------------------------
# ContinuousTrainer
# ---------------------------------------------------------------------------

class ContinuousTrainer:
    """Background learning system.

    Wire-up:
        trainer = ContinuousTrainer(db_path, calibration_path, routing_log_path)
        await trainer.start()

        # In coordinator after successful execution:
        await trainer.record_success(cmd, action_str)

        # On Ctrl-C:
        await trainer.stop()
    """

    def __init__(
        self,
        db_path: Path | str = Path("trainer.db"),
        calibration_path: Path | str = Path("gesture_calibration.json"),
        routing_log_path: Path | str = Path("routing_log.jsonl"),
        adaptation_interval_s: float = 300.0,    # 5 minutes
        hotword_threshold: int = 3,
        gesture_samples_min: int = 10,
        cloud_escalation_limit: float = 0.30,
        local_failure_limit: float = 0.10,
        gate1_relaxation_step: float = 0.05,
        config: Optional["CoordinatorConfig"] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._cal_path = Path(calibration_path)
        self._log_path = Path(routing_log_path)
        self._interval = adaptation_interval_s
        self._hotword_threshold = hotword_threshold
        self._gesture_min = gesture_samples_min
        self._cloud_limit = cloud_escalation_limit
        self._failure_limit = local_failure_limit
        self._gate1_step = gate1_relaxation_step
        self._config = config

        self._db: Optional["aiosqlite.Connection"] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # In-memory gesture confidence samples: {gesture: [conf, ...]}
        self._gesture_samples: dict[str, list[float]] = {}
        self._calibration: dict = {}

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        if not _AIOSQLITE_AVAILABLE:
            log.warning("ContinuousTrainer: aiosqlite unavailable — not starting.")
            return
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._calibration = self._load_calibration()
        self._running = True
        self._task = asyncio.create_task(self._adaptation_loop())
        log.info("ContinuousTrainer started (db=%s)", self._db_path)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save_calibration()
        if self._db:
            await self._db.close()
        log.info("ContinuousTrainer stopped — calibration saved to %s", self._cal_path)

    # ---------------------------------------------------------------------- #
    # Public API — called by HybridCoordinator
    # ---------------------------------------------------------------------- #

    async def record_success(
        self,
        cmd: "Command",
        action_str: str,
        domain: str = "command",
    ) -> None:
        """Record a successfully executed command as a few-shot example."""
        if not self._db:
            return
        now = time.time()
        try:
            await self._db.execute(
                """
                INSERT INTO examples (text, action, source, domain, ts, usage_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT DO NOTHING
                """,
                (cmd.text, action_str, cmd.source, domain, now),
            )
            await self._db.execute(
                """
                UPDATE examples SET usage_count = usage_count + 1, ts = ?
                WHERE text = ? AND action = ?
                """,
                (now, cmd.text, action_str),
            )
            # Word count tracking for hotwords
            for word in _tokens(cmd.text):
                if len(word) >= 3:  # skip very short tokens
                    await self._db.execute(
                        """
                        INSERT INTO word_counts (word, count) VALUES (?, 1)
                        ON CONFLICT(word) DO UPDATE SET count = count + 1
                        """,
                        (word,),
                    )
            await self._db.commit()

            # Gesture confidence tracking
            if cmd.source == "gesture" and hasattr(cmd, "gesture_confidence"):
                gesture = cmd.params.get("gesture", "UNKNOWN")
                self._gesture_samples.setdefault(gesture, []).append(
                    cmd.gesture_confidence
                )
        except Exception as exc:
            log.warning("ContinuousTrainer.record_success error: %s", exc)

    async def get_few_shot_examples(
        self,
        cmd: "Command",
        n: int = 5,
        domain: str = "command",
    ) -> list[dict]:
        """Return the n best few-shot examples for this command, ranked by
        token overlap × recency × log(usage_count)."""
        if not self._db:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(cmd.text)
            # Fetch candidates filtered by domain for better relevance
            async with self._db.execute(
                "SELECT text, action, ts, usage_count FROM examples "
                "WHERE domain = ? ORDER BY ts DESC LIMIT 1000",
                (domain,),
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]

            scored = sorted(
                rows,
                key=lambda r: _score(r, query_tokens, now),
                reverse=True,
            )
            return [
                {"command_text": r["text"], "action_text": r["action"]}
                for r in scored[:n]
                if _score(r, query_tokens, now) > 0.0
            ]
        except Exception as exc:
            log.warning("ContinuousTrainer.get_few_shot_examples error: %s", exc)
            return []

    async def get_hotwords(self) -> list[str]:
        """Return the current Whisper hotwords list."""
        if not self._db:
            return []
        try:
            async with self._db.execute("SELECT word FROM hotwords") as cur:
                return [r["word"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("ContinuousTrainer.get_hotwords error: %s", exc)
            return []

    def record_gesture_sample(self, gesture: str, confidence: float) -> None:
        """Record a gesture confidence observation for calibration."""
        self._gesture_samples.setdefault(gesture, []).append(confidence)

    def get_gesture_floor(self, gesture: str) -> float:
        """Return the calibrated confidence floor for a gesture (default 0.60)."""
        return self._calibration.get("gestures", {}).get(gesture, {}).get(
            "confidence_floor", 0.60
        )

    # ---------------------------------------------------------------------- #
    # Correction feedback — feeds AgentCore fallback memory
    # ---------------------------------------------------------------------- #

    async def record_correction(
        self,
        cmd: "Command",
        wrong_action: str,
        correct_action: str,
    ) -> None:
        """Record a user correction and forward it to the AgentCore fallback agent.

        This does two things:
        1. Stores the correction locally in the few-shot DB (so local inference
           also benefits from the correction).
        2. Sends the correction to the AgentCore fallback agent's long-term
           memory (so cloud disambiguation improves over time).

        Called by the coordinator when the user explicitly corrects an action
        (e.g. via iPad command pad "undo + correct" flow).
        """
        # 1. Store locally as a few-shot example (correct mapping)
        if self._db:
            try:
                now = time.time()
                await self._db.execute(
                    """
                    INSERT INTO examples (text, action, source, domain, ts, usage_count)
                    VALUES (?, ?, ?, 'command', ?, 1)
                    ON CONFLICT DO UPDATE SET action = ?, ts = ?, usage_count = usage_count + 1
                    """,
                    (cmd.text, correct_action, cmd.source, now, correct_action, now),
                )
                await self._db.commit()
                log.info(
                    "Correction stored locally: %r → %s (was %s)",
                    cmd.text, correct_action, wrong_action,
                )
            except Exception as exc:
                log.warning("record_correction local DB error: %s", exc)

        # 2. Forward to AgentCore fallback agent (async, fire-and-forget)
        asyncio.create_task(
            self._send_correction_to_agentcore(cmd.text, wrong_action, correct_action)
        )

    async def _send_correction_to_agentcore(
        self,
        original_text: str,
        wrong_action: str,
        correct_action: str,
    ) -> None:
        """Send a correction to the AgentCore fallback agent's memory.

        Non-blocking: failures are logged but don't affect the main pipeline.
        """
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
            log.debug("AgentCore fallback client not available — correction stored locally only")
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
        log_entries = self._read_routing_log()
        if log_entries:
            self._adapt_gate1_threshold(log_entries)
            await self._promote_hotwords()
        self._update_gesture_calibration()

    def _read_routing_log(self) -> list[dict]:
        if not self._log_path.exists():
            return []
        entries = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError as exc:
            log.warning("ContinuousTrainer: can't read routing log: %s", exc)
        return entries

    def _adapt_gate1_threshold(self, entries: list[dict]) -> None:
        """Requirement 14.3 — relax Gate 1 threshold when cloud escalation is high."""
        if not self._config:
            return

        routed = [e for e in entries if e.get("route") in ("local", "cloud")]
        if len(routed) < 20:  # not enough data
            return

        cloud_count = sum(1 for e in routed if e.get("route") == "cloud")
        cloud_rate = cloud_count / len(routed)

        error_count = sum(1 for e in entries if e.get("action", "").startswith("CLARIFY"))
        failure_rate = error_count / len(entries) if entries else 0.0

        if cloud_rate > self._cloud_limit and failure_rate < self._failure_limit:
            old = self._config.whisper_logprob_min
            self._config.whisper_logprob_min = min(
                -0.1, old + self._gate1_step  # don't go above -0.1 (too permissive)
            )
            log.info(
                "Gate 1 threshold relaxed: %.2f → %.2f "
                "(cloud_rate=%.0f%% failure_rate=%.0f%%)",
                old, self._config.whisper_logprob_min,
                cloud_rate * 100, failure_rate * 100,
            )

    async def _promote_hotwords(self) -> None:
        """Requirement 14.4 — promote words with ≥3 successful uses to hotwords."""
        if not self._db:
            return
        try:
            async with self._db.execute(
                "SELECT word FROM word_counts WHERE count >= ?",
                (self._hotword_threshold,),
            ) as cur:
                candidates = [r["word"] for r in await cur.fetchall()]

            for word in candidates:
                await self._db.execute(
                    "INSERT OR IGNORE INTO hotwords (word, added) VALUES (?, ?)",
                    (word, time.time()),
                )
            if candidates:
                await self._db.commit()
                log.debug("Hotwords updated: %d candidates checked", len(candidates))
        except Exception as exc:
            log.warning("ContinuousTrainer._promote_hotwords error: %s", exc)

    def _update_gesture_calibration(self) -> None:
        """Requirement 14.5 — set gesture floor to p10(observed) - 0.05."""
        updated = False
        for gesture, samples in self._gesture_samples.items():
            if len(samples) < self._gesture_min:
                continue
            samples_sorted = sorted(samples)
            p10_idx = max(0, int(len(samples_sorted) * 0.10) - 1)
            p10 = samples_sorted[p10_idx]
            floor = max(0.0, p10 - 0.05)

            gestures = self._calibration.setdefault("gestures", {})
            entry = gestures.setdefault(gesture, {})
            old_floor = entry.get("confidence_floor", 0.60)
            entry["confidence_floor"] = floor
            entry["sample_count"] = len(samples)
            # Keep up to 100 most recent samples in calibration file
            entry["samples"] = samples_sorted[-100:]
            updated = True
            if abs(floor - old_floor) > 0.001:
                log.info(
                    "Gesture %s confidence floor: %.3f → %.3f (%d samples)",
                    gesture, old_floor, floor, len(samples),
                )
        if updated:
            self._save_calibration()

    # ---------------------------------------------------------------------- #
    # Calibration persistence
    # ---------------------------------------------------------------------- #

    def _load_calibration(self) -> dict:
        if not self._cal_path.exists():
            return {}
        try:
            with self._cal_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Restore in-memory gesture samples from stored calibration
            for gesture, entry in data.get("gestures", {}).items():
                self._gesture_samples[gesture] = list(entry.get("samples", []))
            log.info("Gesture calibration loaded from %s", self._cal_path)
            return data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load gesture calibration: %s", exc)
            return {}

    def _save_calibration(self) -> None:
        try:
            self._calibration["saved_at"] = time.time()
            with self._cal_path.open("w", encoding="utf-8") as fh:
                json.dump(self._calibration, fh, indent=2)
        except OSError as exc:
            log.warning("Could not save gesture calibration: %s", exc)
