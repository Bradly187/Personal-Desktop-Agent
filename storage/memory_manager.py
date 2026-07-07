"""MemoryManager — thin syscall abstraction over AgentDB + SemanticMemory.

Exposes three canonical operations that any agent component can call without
knowing the 29-table AgentDB schema:

    read_context(query, namespace, n)   -> list[dict]   semantic few-shot retrieval
    write_state(key, value, namespace)  -> None         validated structured writes
    search_semantic(query, n)           -> list[dict]   cross-namespace semantic search

Hot-path accessors (called many times per second by ResourceGovernor):
    get_pain_day_active()   -> bool    zero-copy attribute read, no await
    get_pain_day_score()    -> float   zero-copy attribute read, no await

Schema validation: write_state() rejects any (key, namespace) pair not listed in
_VALID_KEYS.  Callers get an error log, not a silent write to the wrong table.

Migration strategy:
    Phase A: instantiate MemoryManager, wire into HybridCoordinator + DevAgent
             (no callers migrated yet — behaviour unchanged)
    Phase B: migrate DevAgent._persist_run / _persist_step to write_state()
    Phase C: migrate ContinuousTrainer.record_success to write_state()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from storage.db import AgentDB
    from storage.semantic_memory import SemanticMemory
    from adaptive.behavioral_twin_state import BehavioralTwinState

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema validation table
# ---------------------------------------------------------------------------

_VALID_KEYS: dict[str, frozenset[str]] = {
    "accessibility": frozenset({
        "few_shot_example",   # (cmd, action_str, domain, command_id)
        "gesture_sample",     # (gesture, confidence, lidar_depth, command_id)
        "gesture_velocity",   # (name, velocity, pain_day)
        "command_outcome",    # AgentDB insert_command (delegated via coordinator)
        "inference_record",   # AgentDB insert_inference kwargs dict
    }),
    "dev_agent": frozenset({
        "agent_run",          # AgentDB insert_agent_run kwargs dict
        "agent_step",         # AgentDB insert_agent_step kwargs dict
        "inference_record",   # AgentDB insert_inference kwargs dict
    }),
    "system": frozenset({
        "pain_day_score",     # log_pain_day kwargs dict (session_id, score, active, ...)
        "voice_profile",      # AgentDB upsert_voice_profile(dict)
        "sensor_telemetry",   # AgentDB insert_sensor_telemetry(**kwargs)
    }),
    "memory": frozenset({
        "memory_note",        # dict: kind, goal, summary, domain, source_run_id, source_command_id
    }),
    # NB: "session_event" was removed — it had no backing AgentDB method and no
    # caller, so a write_state("session_event", ...) validated and then silently
    # dropped. It now fails loudly via _validate_write instead.
}


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------

class MemoryManager:
    """Facade over AgentDB + SemanticMemory.

    Wire-up:
        memory = MemoryManager(agent_db=db, twin_state=twin)
        coordinator.set_memory(memory)
        dev_agent.set_memory(memory)
    """

    def __init__(
        self,
        agent_db: "AgentDB",
        semantic_memory: Optional["SemanticMemory"] = None,
        twin_state: Optional["BehavioralTwinState"] = None,
        graph_memory: Optional[Any] = None,
    ) -> None:
        self._db = agent_db
        self._semantic = semantic_memory
        self._twin = twin_state
        self._graph = graph_memory

    def set_twin_state(self, twin: "BehavioralTwinState") -> None:
        self._twin = twin

    def set_semantic_memory(self, semantic: "SemanticMemory") -> None:
        self._semantic = semantic

    def set_graph_memory(self, graph_memory: Any) -> None:
        self._graph = graph_memory

    # ── Hot-path pain-day accessors (zero-copy, no await) ─────────────────────

    def get_pain_day_active(self) -> bool:
        """Return current pain-day flag. Zero-copy — never hits the DB."""
        if self._twin is None:
            return False
        return bool(self._twin._pain_day_active)

    def get_pain_day_score(self) -> float:
        """Return current pain-day score [0.0, 1.0]. Zero-copy — never hits the DB."""
        if self._twin is None:
            return 0.0
        return float(self._twin._pain_day_score)

    # ── Async reads ───────────────────────────────────────────────────────────

    async def read_context(
        self,
        query: str,
        namespace: str = "accessibility",
        n: int = 5,
    ) -> list[dict]:
        """Return n semantically similar past commands for this namespace.

        Tries SemanticMemory (ChromaDB) first; falls back to AgentDB few-shot
        retrieval when ChromaDB is unavailable.
        """
        try:
            # The ChromaDB behavioral_memory collection holds ONLY accessibility-
            # namespace docs (BehavioralTwinState.observe adds to SemanticMemory
            # exclusively in the accessibility branch), so it is effectively the
            # accessibility store: use it only for accessibility reads. Namespaced
            # reads (dev_agent/system) route to the AgentDB few-shot table, which
            # is domain-filtered and therefore namespace-correct.
            if (
                namespace == "accessibility"
                and self._semantic is not None
                and self._semantic.available
            ):
                return await self._semantic.query_similar(query, n)
            if self._db is not None and self._db.available:
                # Minimal Command-like stub for the DB query. NB: the scorer in
                # AgentDB.memory.get_few_shot_examples reads only `.text`; the remaining
                # fields are inert padding to satisfy attribute access and do not
                # influence ranking.
                class _FakeCmd:
                    text = query
                    source = "voice"
                    whisper_logprob = 0.0
                    gesture_confidence = 1.0
                    session_context: list = []
                    params: dict = {}
                return await self._db.memory.get_few_shot_examples(
                    _FakeCmd(), n=n, domain=namespace
                )
        except Exception as exc:
            log.warning("MemoryManager.read_context(%r) failed: %s", query, exc)
        return []

    async def search_semantic(self, query: str, n: int = 5) -> list[dict]:
        """Semantic search across the accessibility namespace. Identical to
        read_context with namespace='accessibility'."""
        return await self.read_context(query, namespace="accessibility", n=n)

    # ── Episodic / archival memory (R-2) ──────────────────────────────────────

    async def recall_episodic(
        self,
        query: str,
        *,
        n: int = 5,
        kind: Optional[str] = None,
        domain: Optional[str] = None,
        pain_day: Optional[bool] = None,
    ) -> list[dict]:
        """Recall episodic "how I solved X under state Y" notes for this query.

        Local-only (agent.db); never hits the cloud. Returns [] gracefully when
        the DB is unavailable or empty. Touches recalled rows so salience tracks use.
        """
        if self._db is None or not self._db.available:
            return []
        try:
            hits = await self._db.memory.query_episodic_memory(
                query, n=n, kind=kind, domain=domain, pain_day=pain_day
            )
            for h in hits:
                try:
                    await self._db.memory.touch_episodic_memory(h["id"])
                except Exception:
                    pass
            return hits
        except Exception as exc:
            log.warning("MemoryManager.recall_episodic(%r) failed: %s", query, exc)
            return []

    async def write_memory_note(
        self,
        *,
        kind: str,
        goal: str,
        summary: str,
        domain: str = "general",
        source_run_id: Optional[int] = None,
        source_command_id: Optional[int] = None,
    ) -> Optional[int]:
        """Persist one episodic memory note, tagged with the CURRENT physical state.

        The pain-day tag is read zero-copy from the twin so the note records the
        state the user was in when this episode happened.
        """
        if self._db is None:
            return None
        try:
            return await self._db.memory.insert_episodic_memory(
                kind, goal, summary,
                domain=domain,
                source_run_id=source_run_id,
                source_command_id=source_command_id,
                pain_day_active=self.get_pain_day_active(),
                pain_day_score=self.get_pain_day_score(),
            )
        except Exception as exc:
            log.warning("MemoryManager.write_memory_note(%r) failed: %s", goal, exc)
            return None

    async def write_graph_fact(
        self,
        source_name: str,
        source_type: str,
        relation: str,
        target_name: str,
        target_type: str,
        weight: float = 1.0,
    ) -> None:
        """Persist a factual relationship in the Knowledge Graph."""
        if self._graph is None:
            return
        await self._graph.add_fact(source_name, source_type, relation, target_name, target_type, weight)

    def query_graph(self, node_name: str, depth: int = 1) -> list[dict]:
        """Find immediate neighbors of a node by name in the Knowledge Graph."""
        if self._graph is None:
            return []
        return self._graph.get_neighbors(node_name, depth)

    # ── Async writes ──────────────────────────────────────────────────────────

    async def write_state(
        self,
        key: str,
        value: Any,
        namespace: str = "accessibility",
    ) -> None:
        """Write a structured state record.

        Validates (key, namespace) against the schema table.  Invalid pairs are
        logged as errors and dropped — never silently written to the wrong table.
        """
        if not self._validate_write(key, namespace):
            log.error(
                "MemoryManager: invalid write key=%r for namespace=%r — dropped",
                key, namespace,
            )
            return
        try:
            await self._dispatch_write(key, value, namespace)
        except Exception as exc:
            log.warning(
                "MemoryManager.write_state(key=%r, namespace=%r) failed: %s",
                key, namespace, exc,
            )

    def _validate_write(self, key: str, namespace: str) -> bool:
        return key in _VALID_KEYS.get(namespace, frozenset())

    async def _dispatch_write(
        self, key: str, value: Any, namespace: str
    ) -> None:
        """Route a validated write to the appropriate AgentDB method."""
        if key == "few_shot_example":
            cmd, action_str, domain, command_id = value
            await self._db.memory.upsert_few_shot_example(cmd, action_str, domain, command_id)

        elif key == "gesture_sample":
            gesture, conf, lidar_depth, command_id = value
            await self._db.gestures.record_gesture_sample(
                gesture, conf,
                lidar_depth_m=lidar_depth,
                command_id=command_id,
            )

        elif key == "gesture_velocity":
            name, vel, pain_day = value
            await self._db.gestures.record_gesture_velocity(name, vel, pain_day=pain_day)

        elif key == "inference_record":
            # value is a dict matching insert_inference kwargs
            await self._db.inferences.insert_inference(**value)

        elif key == "agent_run":
            # value is a dict matching insert_agent_run kwargs
            await self._db.runs.insert_agent_run(**value)

        elif key == "agent_step":
            # value is a dict matching insert_agent_step kwargs
            await self._db.runs.insert_agent_step(**value)

        elif key == "sensor_telemetry":
            # value is a dict matching insert_sensor_telemetry kwargs
            # (session_id, ts, + optional per-sensor columns)
            await self._db.telemetry.insert_sensor_telemetry(**value)

        elif key == "voice_profile":
            # value is a single dict (NOT **kwargs) — see AgentDB.voice.upsert_voice_profile
            await self._db.voice.upsert_voice_profile(value)

        elif key == "pain_day_score":
            # value is a dict: {session_id, score, active, fail_ratio,
            # clarify_ratio, [gesture_conf_delta], [cmd_rate_delta]}
            v = value
            await self._db.profile.log_pain_day(
                session_id=v["session_id"],
                score=v["score"],
                active=v["active"],
                fail_ratio=v.get("fail_ratio", 0.0),
                clarify_ratio=v.get("clarify_ratio", 0.0),
                gesture_conf_delta=v.get("gesture_conf_delta", 0.0),
                cmd_rate_delta=v.get("cmd_rate_delta", 0.0),
            )

        elif key == "memory_note":
            # value is a dict matching write_memory_note kwargs
            await self.write_memory_note(**value)

        elif key == "command_outcome":
            # Intentional no-op: commands are inserted by HybridCoordinator via
            # AgentDB.commands.insert_command (which needs the returned command_id).
            # Listed in _VALID_KEYS so a stray write validates rather than
            # erroring; it must not double-insert here.
            pass

        else:
            log.debug(
                "MemoryManager._dispatch_write: unhandled key=%r (namespace=%r)",
                key, namespace,
            )

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "db_available": self._db.available if self._db else False,
            "semantic_available": (
                self._semantic.available if self._semantic else False
            ),
            "pain_day_active": self.get_pain_day_active(),
            "pain_day_score": round(self.get_pain_day_score(), 3),
        }
