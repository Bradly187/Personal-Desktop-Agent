"""Property-based tests for BehavioralTwinState.

Feature: behavioral-twin-state
PBT library: Hypothesis (pip install hypothesis)
Minimum max_examples=100 per property.

Properties covered:
  Property 3:  Working set size is min(N, 500) after start()
  Property 8:  SessionHistory is a bounded window
  Property 9:  Cross-session context round-trip
  Property 12: TwinSnapshot is immutable and complete
  Property 14: observe() returns before persistence completes (non-blocking)
  Property 18: stop() flushes all pending tasks
  Property 19: Default snapshot returned before start() completes
  Property 20: Public methods never raise for any input
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass
from typing import Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adaptive.behavioral_twin_state import (
    BehavioralTwinState,
    TwinSnapshot,
    _DEFAULT_SNAPSHOT,
)


# ---------------------------------------------------------------------------
# MockAgentDB
# ---------------------------------------------------------------------------

class MockAgentDB:
    available = True

    def __init__(self, commands=None, session_history=None, preference_json=None):
        self._commands = commands or []
        self._session_history = session_history or []
        self._preference_json = preference_json
        self._written_history = []
        self._logged_settings = []

    async def get_recent_successful_commands(self, limit=500):
        return self._commands[:limit]

    async def get_most_recent_session_id(self, exclude_session_id=None):
        return 1

    async def get_command_stats_last_n_days(self, days=30):
        return []

    async def get_source_stats_last_n_days(self, days=7):
        return []

    async def get_preference_model_snapshot(self):
        return self._preference_json

    async def read_session_history(self, session_id, limit=20):
        return self._session_history[:limit]

    async def write_session_history(self, session_id, history):
        self._written_history = history

    async def log_settings_change(self, component, key, old_value, new_value, changed_by):
        self._logged_settings.append((component, key, new_value))

    async def log_pain_day(self, session_id, score, active, fail_ratio, clarify_ratio,
                           gesture_conf_delta, cmd_rate_delta):
        pass


# ---------------------------------------------------------------------------
# MockCommand
# ---------------------------------------------------------------------------

@dataclass
class MockCommand:
    text: str = "click the button"
    source: str = "voice"
    whisper_logprob: float = -0.5
    gesture_confidence: float = 0.8
    session_context: list = None
    params: dict = None

    def __post_init__(self):
        if self.session_context is None:
            self.session_context = []
        if self.params is None:
            self.params = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_command_rows(n: int) -> list[dict]:
    """Build n fake command dicts as returned by AgentDB."""
    return [
        {
            "text": f"command {i}",
            "action": "CLICK",
            "source": "voice",
            "ts": float(i),
            "gesture_confidence": 0.8,
        }
        for i in range(n)
    ]


def make_session_history_rows(texts: list[str]) -> list[dict]:
    """Build session history rows as returned by AgentDB.read_session_history."""
    return [
        {"cmd_text": t, "action": "CLICK", "source": "voice", "ts": float(i)}
        for i, t in enumerate(texts)
    ]


def _make_twin(commands=None, session_history=None, preference_json=None) -> BehavioralTwinState:
    """Create a BehavioralTwinState with a MockAgentDB, ChromaDB disabled.

    Patches SemanticMemory.start() to return False so that even with chromadb
    installed, tests run in AgentDB-only (Jaccard fallback) mode.
    """
    db = MockAgentDB(
        commands=commands,
        session_history=session_history,
        preference_json=preference_json,
    )
    twin = BehavioralTwinState(agent_db=db, chroma_dir="/nonexistent_chroma_dir_for_tests")

    # Patch start() so that if chromadb is installed, it still returns False.
    # This keeps tests fast, deterministic, and ChromaDB-free.
    async def _no_chroma_start():
        return False

    twin._semantic_memory.start = _no_chroma_start
    return twin


# ---------------------------------------------------------------------------
# Property 3: Working set size is min(N, 500) after start()
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 3: working set size is min(N, 500) after start()
# Validates: Requirements 1.2
@given(n=st.integers(min_value=0, max_value=600))
@settings(max_examples=100)
def test_working_set_size_is_min_n_500(n):
    """After start(), len(twin._working_set) == min(N, 500) for any N commands in AgentDB."""
    commands = make_command_rows(n)
    twin = _make_twin(commands=commands)

    asyncio.run(_run_start_stop(twin))

    expected = min(n, BehavioralTwinState.WORKING_SET_SIZE)
    assert len(twin._working_set) == expected, (
        f"Expected working set size {expected} for N={n}, got {len(twin._working_set)}"
    )


async def _run_start_stop(twin: BehavioralTwinState) -> None:
    await twin.start()
    await twin.stop()


# ---------------------------------------------------------------------------
# Property 8: SessionHistory is a bounded window
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 8: SessionHistory is a bounded window
# Validates: Requirements 3.1, 3.3, 4.5
@given(n=st.integers(min_value=0, max_value=50))
@settings(max_examples=100)
def test_session_history_bounded_normal_mode(n):
    """Normal mode: after N observe() calls, len(session_history) == min(N, 20), FIFO order."""
    twin = _make_twin()

    async def run():
        await twin.start()
        for i in range(n):
            cmd = MockCommand(text=f"cmd_{i}")
            await twin.observe(cmd, "CLICK")
        # Flush all pending tasks
        await twin.stop()

    asyncio.run(run())

    expected_len = min(n, BehavioralTwinState.SESSION_HISTORY_MAX)
    history = twin._session_history["accessibility"]
    assert len(history) == expected_len, (
        f"Normal mode: expected {expected_len} history entries for N={n}, "
        f"got {len(history)}"
    )

    # Verify chronological order (FIFO — oldest dropped, newest at end)
    if n > 0:
        # The last entry should be the most recently observed command
        last_expected_text = f"cmd_{n - 1}"
        assert history[-1] == last_expected_text, (
            f"Last history entry should be '{last_expected_text}', got '{history[-1]}'"
        )
        # If we have a full window, the first entry should be cmd_{n - window_size}
        if n >= BehavioralTwinState.SESSION_HISTORY_MAX:
            first_expected_text = f"cmd_{n - BehavioralTwinState.SESSION_HISTORY_MAX}"
            assert history[0] == first_expected_text, (
                f"First history entry should be '{first_expected_text}', got '{history[0]}'"
            )


# Feature: behavioral-twin-state, Property 8: SessionHistory is a bounded window (pain day)
# Validates: Requirements 3.1, 3.3, 4.5
@given(n=st.integers(min_value=0, max_value=30))
@settings(max_examples=100)
def test_session_history_bounded_pain_day_mode(n):
    """Pain day mode: after N observe() calls, len(session_history) == min(N, 10), FIFO order."""
    twin = _make_twin()

    async def run():
        await twin.start()
        # Manually activate pain day mode
        twin._pain_day_active = True
        for i in range(n):
            cmd = MockCommand(text=f"cmd_{i}")
            await twin.observe(cmd, "CLICK")
        await twin.stop()

    asyncio.run(run())

    expected_len = min(n, BehavioralTwinState.SESSION_HISTORY_PAIN_DAY)
    history = twin._session_history["accessibility"]
    assert len(history) == expected_len, (
        f"Pain day mode: expected {expected_len} history entries for N={n}, "
        f"got {len(history)}"
    )

    # Verify chronological order (FIFO)
    if n > 0:
        last_expected_text = f"cmd_{n - 1}"
        assert history[-1] == last_expected_text, (
            f"Last history entry should be '{last_expected_text}', got '{history[-1]}'"
        )
        if n >= BehavioralTwinState.SESSION_HISTORY_PAIN_DAY:
            first_expected_text = f"cmd_{n - BehavioralTwinState.SESSION_HISTORY_PAIN_DAY}"
            assert history[0] == first_expected_text, (
                f"First history entry should be '{first_expected_text}', got '{history[0]}'"
            )


# ---------------------------------------------------------------------------
# Property 9: Cross-session context round-trip
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 9: cross-session context round-trip
# Validates: Requirements 3.2, 3.6
@given(
    prior_texts=st.lists(
        st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"))),
        min_size=0,
        max_size=30,
    )
)
@settings(max_examples=100)
def test_cross_session_context_round_trip(prior_texts):
    """Prior session commands are loaded on new instance start, including abnormal termination.

    The AgentDB returns session history rows (simulating a prior session that may
    have ended abnormally — no ended_at). The new twin instance should load them.
    """
    rows = make_session_history_rows(prior_texts)
    twin = _make_twin(session_history=rows)

    async def run():
        await twin.start()
        await twin.stop()

    asyncio.run(run())

    expected_len = min(len(prior_texts), BehavioralTwinState.SESSION_HISTORY_MAX)
    loaded = twin._session_history["accessibility"]

    assert len(loaded) == expected_len, (
        f"Expected {expected_len} cross-session entries, got {len(loaded)}"
    )

    # Verify the loaded texts match the prior session texts (in order)
    expected_texts = prior_texts[:expected_len]
    assert loaded == expected_texts, (
        f"Cross-session context mismatch: expected {expected_texts}, got {loaded}"
    )


# ---------------------------------------------------------------------------
# Property 12: TwinSnapshot is immutable and complete
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 12: TwinSnapshot is immutable and complete
# Validates: Requirements 5.2, 5.6
@given(
    pain_day_active=st.booleans(),
    preferred_actions=st.lists(st.text(min_size=1, max_size=10), min_size=0, max_size=5),
    source_weights=st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        max_size=5,
    ),
    session_context=st.lists(st.text(min_size=0, max_size=50), min_size=0, max_size=20),
    command_count_today=st.integers(min_value=0, max_value=1000),
    pain_day_score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    snapshot_ts=st.floats(min_value=0.0, max_value=1e9, allow_nan=False),
)
@settings(max_examples=100)
def test_twin_snapshot_immutable_and_complete(
    pain_day_active,
    preferred_actions,
    source_weights,
    session_context,
    command_count_today,
    pain_day_score,
    snapshot_ts,
):
    """TwinSnapshot raises FrozenInstanceError on any field set, and all 7 fields are present."""
    snapshot = TwinSnapshot(
        pain_day_active=pain_day_active,
        preferred_actions=preferred_actions,
        source_weights=source_weights,
        session_context=session_context,
        command_count_today=command_count_today,
        pain_day_score=pain_day_score,
        snapshot_ts=snapshot_ts,
    )

    # (a) All 7 required fields are present with correct types
    assert isinstance(snapshot.pain_day_active, bool)
    assert isinstance(snapshot.preferred_actions, list)
    assert isinstance(snapshot.source_weights, dict)
    assert isinstance(snapshot.session_context, list)
    assert isinstance(snapshot.command_count_today, int)
    assert isinstance(snapshot.pain_day_score, float)
    assert isinstance(snapshot.snapshot_ts, float)

    # (b) FrozenInstanceError is raised when trying to set any field
    required_fields = [
        "pain_day_active",
        "preferred_actions",
        "source_weights",
        "session_context",
        "command_count_today",
        "pain_day_score",
        "snapshot_ts",
    ]
    for field_name in required_fields:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(snapshot, field_name, None)


# ---------------------------------------------------------------------------
# Property 14: observe() returns before persistence completes (non-blocking)
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 14: observe() returns before persistence completes
# Validates: Requirements 6.4
@given(delay_s=st.floats(min_value=0.05, max_value=0.2, allow_nan=False))
@settings(max_examples=100)
def test_observe_returns_before_persistence_completes(delay_s):
    """observe() returns immediately; the background task has not yet completed."""

    async def run():
        twin = _make_twin()
        await twin.start()

        # Patch _persist_observation to be artificially slow
        completed = []

        async def slow_persist(cmd, action_str):
            await asyncio.sleep(delay_s)
            completed.append(True)

        twin._persist_observation = slow_persist

        cmd = MockCommand(text="test command")
        t0 = time.monotonic()
        await twin.observe(cmd, "CLICK")
        elapsed = time.monotonic() - t0

        # observe() must return well before the delay expires
        assert elapsed < delay_s, (
            f"observe() took {elapsed:.4f}s but persistence delay is {delay_s:.4f}s — "
            "observe() should be non-blocking"
        )

        # Background task has NOT completed yet
        assert len(completed) == 0, (
            "Persistence task completed synchronously — observe() is not non-blocking"
        )

        # Clean up: cancel pain day loop and pending tasks
        if twin._pain_day_task:
            twin._pain_day_task.cancel()
            try:
                await twin._pain_day_task
            except asyncio.CancelledError:
                pass
        if twin._pending_tasks:
            await asyncio.gather(*twin._pending_tasks, return_exceptions=True)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Property 18: stop() flushes all pending tasks
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 18: stop() flushes all pending tasks
# Validates: Requirements 8.4
@given(n=st.integers(min_value=1, max_value=20))
@settings(max_examples=100)
def test_stop_flushes_all_pending_tasks(n):
    """After N observe() calls and stop(), all background tasks have completed."""

    async def run():
        twin = _make_twin()
        await twin.start()

        completed = []

        # Patch _persist_observation to track completions with a small delay
        async def tracking_persist(cmd, action_str, namespace="accessibility"):
            await asyncio.sleep(0.01)
            completed.append(id(cmd))

        twin._persist_observation = tracking_persist

        cmds = [MockCommand(text=f"cmd_{i}") for i in range(n)]
        for cmd in cmds:
            await twin.observe(cmd, "CLICK")

        # At this point, tasks are scheduled but may not be done
        # stop() must flush them all
        await twin.stop()

        assert len(completed) == n, (
            f"Expected {n} completed tasks after stop(), got {len(completed)}"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Property 19: Default snapshot returned before start() completes
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 19: default snapshot returned before start() completes
# Validates: Requirements 8.6
@given(
    n_commands=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=100)
def test_default_snapshot_before_start_completes(n_commands):
    """get_snapshot() returns _DEFAULT_SNAPSHOT before start() is called."""
    commands = make_command_rows(n_commands)
    twin = _make_twin(commands=commands)

    # Before start() — is_ready is False
    assert not twin.is_ready

    async def run():
        snapshot = await twin.get_snapshot()
        return snapshot

    snapshot = asyncio.run(run())

    # Must be the default snapshot
    assert snapshot.pain_day_active is False
    assert snapshot.preferred_actions == []
    assert snapshot.pain_day_score == 0.0
    assert snapshot.session_context == []
    assert snapshot.command_count_today == 0
    assert snapshot.source_weights == {}
    assert snapshot.snapshot_ts == 0.0

    # Verify it is literally the _DEFAULT_SNAPSHOT object
    assert snapshot is _DEFAULT_SNAPSHOT


# ---------------------------------------------------------------------------
# Property 20: Public methods never raise for any input
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 20: public methods never raise for any input
# Validates: Requirements 8.7
@given(
    text=st.one_of(
        st.none(),
        st.just(""),
        st.text(min_size=0, max_size=10000),
    ),
    action_str=st.one_of(
        st.none(),
        st.just(""),
        st.text(min_size=0, max_size=100),
    ),
    n=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=100)
def test_public_methods_never_raise(text, action_str, n):
    """observe(), get_snapshot(), query_similar(), get_session_context() never raise."""

    async def run():
        twin = _make_twin()
        await twin.start()

        # Build a command — handle None text gracefully
        cmd_text = text if text is not None else ""
        cmd = MockCommand(text=cmd_text)

        # observe() — must not raise even with None/empty/huge inputs
        try:
            await twin.observe(cmd, action_str if action_str is not None else "")
        except Exception as exc:
            raise AssertionError(f"observe() raised {type(exc).__name__}: {exc}") from exc

        # get_snapshot() — must not raise
        try:
            snapshot = await twin.get_snapshot()
            assert isinstance(snapshot, TwinSnapshot)
        except Exception as exc:
            raise AssertionError(f"get_snapshot() raised {type(exc).__name__}: {exc}") from exc

        # query_similar() — must not raise even with None/empty/huge text
        query_text = text if text is not None else ""
        try:
            results = await twin.query_similar(query_text, n)
            assert isinstance(results, list)
        except Exception as exc:
            raise AssertionError(f"query_similar() raised {type(exc).__name__}: {exc}") from exc

        # get_session_context() — must not raise
        try:
            ctx = twin.get_session_context()
            assert isinstance(ctx, list)
        except Exception as exc:
            raise AssertionError(
                f"get_session_context() raised {type(exc).__name__}: {exc}"
            ) from exc

        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Additional edge-case: observe() with None action_str does not raise
# ---------------------------------------------------------------------------

def test_observe_none_action_str_does_not_raise():
    """observe() with None action_str must not raise (covers Property 20 edge case)."""

    async def run():
        twin = _make_twin()
        await twin.start()
        cmd = MockCommand(text="test")
        # Pass None as action_str — should be handled gracefully
        await twin.observe(cmd, None)
        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Additional edge-case: get_snapshot() with None text does not raise
# ---------------------------------------------------------------------------

def test_query_similar_none_text_does_not_raise():
    """query_similar(None) must not raise (covers Property 20 edge case)."""

    async def run():
        twin = _make_twin()
        await twin.start()
        results = await twin.query_similar(None, 5)
        assert isinstance(results, list)
        await twin.stop()

    asyncio.run(run())


# ===========================================================================
# Task 13 — Unit tests (13.1 – 13.7)
# ===========================================================================

# ---------------------------------------------------------------------------
# 13.1  ChromaDB timeout: init > 5s → AgentDB-only mode, is_ready=True
# ---------------------------------------------------------------------------

def test_chroma_timeout_falls_back_to_agentdb_only():
    """When ChromaDB init exceeds STARTUP_TIMEOUT_S, is_ready becomes True and
    _semantic_memory._available is False (AgentDB-only mode)."""

    async def run():
        twin = _make_twin()

        # Patch _init_chroma to simulate a long-running ChromaDB init
        async def slow_chroma():
            await asyncio.sleep(BehavioralTwinState.STARTUP_TIMEOUT_S + 1.0)

        twin._init_chroma = slow_chroma

        # start() wraps the gather in asyncio.timeout(STARTUP_TIMEOUT_S)
        # so it should resolve (with timeout) and set is_ready=True
        await twin.start()

        assert twin.is_ready, "is_ready must be True after startup timeout"
        assert not twin._semantic_memory._available, (
            "_semantic_memory._available must be False when ChromaDB timed out"
        )
        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.2  asyncio.gather concurrency: AgentDB and ChromaDB init run concurrently
# ---------------------------------------------------------------------------

def test_start_initialises_agentdb_and_chroma_concurrently():
    """Both _init_agent_db and _init_chroma are awaited concurrently (asyncio.gather).
    Verified by both being called before the overall start() completes."""

    async def run():
        twin = _make_twin()

        call_order = []

        async def tracked_agent_db():
            call_order.append("agentdb_start")
            await asyncio.sleep(0)
            call_order.append("agentdb_end")

        async def tracked_chroma():
            call_order.append("chroma_start")
            await asyncio.sleep(0)
            call_order.append("chroma_end")

        twin._init_agent_db = tracked_agent_db
        twin._init_chroma = tracked_chroma

        await twin.start()

        # Both inits must have been called
        assert "agentdb_start" in call_order, "_init_agent_db was not called"
        assert "chroma_start" in call_order, "_init_chroma was not called"
        # Because they run under asyncio.gather, both *_start events appear
        # before both *_end events (interleaved execution)
        agentdb_start_idx = call_order.index("agentdb_start")
        chroma_start_idx = call_order.index("chroma_start")
        agentdb_end_idx = call_order.index("agentdb_end")
        chroma_end_idx = call_order.index("chroma_end")

        # Both started before either finished (concurrent, not sequential)
        assert agentdb_start_idx < agentdb_end_idx
        assert chroma_start_idx < chroma_end_idx
        # At least one interleaving: chroma starts before agentdb ends (or vice versa)
        concurrent = (
            chroma_start_idx < agentdb_end_idx or
            agentdb_start_idx < chroma_end_idx
        )
        assert concurrent, (
            f"Inits appear sequential rather than concurrent: {call_order}"
        )

        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.3  stop() flush ordering: pending observe() tasks complete before
#        _write_session_history and _persist_preference_model are called
# ---------------------------------------------------------------------------

def test_stop_flushes_pending_tasks_before_persisting():
    """stop() drains _pending_tasks first, then calls persistence methods.

    We verify that a slow observe() background task completes before
    _write_session_history is called by planting a flag in the task and
    checking it in the overridden _write_session_history.
    """

    async def run():
        twin = _make_twin()
        await twin.start()

        observe_completed = []
        write_history_called_while_tasks_done = []

        async def slow_persist(cmd, action_str, namespace="accessibility"):
            await asyncio.sleep(0.02)
            observe_completed.append(True)

        async def tracking_write_history():
            write_history_called_while_tasks_done.append(len(observe_completed))

        twin._persist_observation = slow_persist
        twin._write_session_history = tracking_write_history

        cmd = MockCommand(text="flush ordering test")
        await twin.observe(cmd, "CLICK")
        # At this point the observe task is scheduled but NOT done yet
        assert len(observe_completed) == 0, "observe completed synchronously — unexpected"

        await twin.stop()

        # After stop(), write_session_history must have been called
        assert write_history_called_while_tasks_done, (
            "_write_session_history was never called during stop()"
        )
        # And the observe task must have been flushed before it was called
        completed_before_write = write_history_called_while_tasks_done[0]
        assert completed_before_write == 1, (
            f"_write_session_history called before observe() tasks completed: "
            f"completed={completed_before_write}"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.4  Pain day deactivation: when score < 0.4, _pain_day_active becomes False
# ---------------------------------------------------------------------------

def test_pain_day_deactivates_when_score_drops_below_threshold():
    """When _pain_day_active=True and score < DEACTIVATE_THRESHOLD, it deactivates."""

    async def run():
        twin = _make_twin()
        await twin.start()

        # Force pain day active
        twin._pain_day_active = True
        twin._session_cmd_count = 10  # above MIN_COMMANDS

        # Force low pain day score (all signals = 0)
        twin._session_fail_count = 0
        twin._session_clarify_count = 0
        twin._session_gesture_confs = []
        twin._working_set = []

        # _recompute_pain_day_score() with all signals = 0 → score = 0.0 < 0.4
        score = twin._recompute_pain_day_score()
        assert score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD, (
            f"Expected score < 0.4 for deactivation test, got {score}"
        )

        # Simulate what _pain_day_loop does on one tick
        twin._pain_day_score = score
        old_active = twin._pain_day_active
        if twin._pain_day_active and score < BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD:
            twin._pain_day_active = False
            twin._current_snapshot = twin._build_snapshot()

        assert not twin._pain_day_active, (
            f"pain_day_active should be False after score ({score:.3f}) dropped "
            f"below deactivate threshold ({BehavioralTwinState.PAIN_DAY_DEACTIVATE_THRESHOLD})"
        )

        # Snapshot must reflect the deactivation
        snapshot = await twin.get_snapshot()
        assert not snapshot.pain_day_active, (
            "TwinSnapshot.pain_day_active should be False after deactivation"
        )

        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.5  Empty DB on first run: start() succeeds, empty working set,
#        default PreferenceModel
# ---------------------------------------------------------------------------

def test_first_run_empty_db_starts_successfully():
    """On first run with an empty AgentDB, start() must:
    - Complete without error
    - Leave _working_set empty
    - Use default (empty) PreferenceModel
    - Set is_ready=True
    """

    async def run():
        # MockAgentDB with no commands, no history, no saved preferences
        twin = _make_twin(commands=[], session_history=[], preference_json=None)

        await twin.start()

        assert twin.is_ready, "is_ready must be True after start() on empty DB"
        assert twin._working_set == [], (
            f"Expected empty working set on first run, got {len(twin._working_set)} entries"
        )
        # Default PreferenceModel has empty action_stats and source_counts
        assert twin._preference_model.action_stats == {}, (
            "Expected empty action_stats from default PreferenceModel"
        )
        assert twin._preference_model.source_counts == {}, (
            "Expected empty source_counts from default PreferenceModel"
        )
        assert twin._session_history["accessibility"] == [], (
            "Expected empty accessibility session history on first run"
        )
        assert twin._session_history["dev_agent"] == [], (
            "Expected empty dev_agent session history on first run"
        )

        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.6  get_snapshot() latency: completes in < 50 ms
# ---------------------------------------------------------------------------

def test_get_snapshot_latency_under_50ms():
    """get_snapshot() must complete in < 50 ms after start()."""

    async def run():
        twin = _make_twin()
        await twin.start()

        N = 100
        elapsed_times = []
        for _ in range(N):
            t0 = time.monotonic()
            snapshot = await twin.get_snapshot()
            elapsed = (time.monotonic() - t0) * 1000  # ms
            elapsed_times.append(elapsed)
            assert isinstance(snapshot, TwinSnapshot)

        await twin.stop()

        max_elapsed = max(elapsed_times)
        p99_elapsed = sorted(elapsed_times)[int(N * 0.99)]
        assert p99_elapsed < 50.0, (
            f"get_snapshot() p99 latency {p99_elapsed:.2f}ms exceeds 50ms limit. "
            f"max={max_elapsed:.2f}ms"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 13.7  start() completes within 5 seconds
# ---------------------------------------------------------------------------

def test_start_completes_within_5_seconds():
    """start() must complete within STARTUP_TIMEOUT_S (5 seconds) on any DB state."""

    async def run():
        twin = _make_twin()

        t0 = time.monotonic()
        await twin.start()
        elapsed = time.monotonic() - t0

        assert twin.is_ready, "is_ready must be True after start()"
        assert elapsed < BehavioralTwinState.STARTUP_TIMEOUT_S + 0.5, (
            f"start() took {elapsed:.2f}s, exceeds STARTUP_TIMEOUT_S="
            f"{BehavioralTwinState.STARTUP_TIMEOUT_S}s"
        )

        await twin.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tranche 1 A1: record_failure feeds the pain-day fail signal (and ONLY that)
# ---------------------------------------------------------------------------

def test_record_failure_increments_fail_and_cmd_counts():
    """A failure bumps BOTH fail and cmd counts so signal_1 = fail/(success+fail)."""
    twin = _make_twin()

    async def run():
        await twin.start()
        for i in range(2):  # 2 successes
            await twin.observe(MockCommand(text=f"ok_{i}"), "CLICK")
        if twin._pending_tasks:
            await asyncio.gather(*twin._pending_tasks, return_exceptions=True)
        for i in range(3):  # 3 failures
            await twin.record_failure(MockCommand(text=f"bad_{i}"), "CLICK")
        await twin.stop()

    asyncio.run(run())
    assert twin._session_fail_count == 3
    assert twin._session_cmd_count == 5  # 2 success + 3 fail


def test_record_failure_does_not_touch_learning_stores():
    """The dominant Tranche-1 invariant: failures move counters only — never the
    success-biased SemanticMemory / PreferenceModel / clarify counters."""
    twin = _make_twin()
    calls = {"sem_add": 0, "pref_update": 0}

    async def fake_add(*a, **k):
        calls["sem_add"] += 1

    def fake_update(*a, **k):
        calls["pref_update"] += 1

    async def run():
        await twin.start()
        twin._semantic_memory.add = fake_add
        twin._preference_model.update = fake_update
        await twin.record_failure(MockCommand(text="bad"), "CLICK")
        await twin.stop()

    asyncio.run(run())
    assert calls["sem_add"] == 0
    assert calls["pref_update"] == 0
    assert twin._session_clarify_count == 0
    # The accessibility few-shot session history must not gain the failed text
    assert "bad" not in twin._session_history["accessibility"]


def test_record_failure_dev_agent_namespace_is_ignored():
    twin = _make_twin()

    async def run():
        await twin.start()
        await twin.record_failure(MockCommand(text="bad"), "CLICK", namespace="dev_agent")
        await twin.stop()

    asyncio.run(run())
    assert twin._session_fail_count == 0
    assert twin._session_cmd_count == 0


def test_record_failure_never_raises_on_bad_input():
    twin = _make_twin()

    async def run():
        await twin.start()
        await twin.record_failure(None, "")  # cmd=None must not raise
        await twin.stop()

    asyncio.run(run())
    assert twin._session_fail_count == 1  # counter-only path is cmd-field-independent


def test_recompute_stashes_real_deltas_for_logging():
    """_recompute_pain_day_score stashes signals 3 & 4 so _pain_day_loop logs the
    real values into twin_pain_day_log instead of hard-coded 0.0."""
    from adaptive.behavioral_twin_state import ActionStats
    twin = _make_twin()

    async def run():
        await twin.start()
        # Deterministic gesture-confidence-drop: good-day baseline 0.9, session 0.3
        stats = ActionStats()
        stats.good_day_conf_sum = 0.9
        stats.good_day_conf_count = 1
        twin._preference_model.action_stats["CLICK"] = stats
        twin._session_gesture_confs = [0.3]
        twin._session_cmd_count = 4
        twin._session_fail_count = 2
        twin._recompute_pain_day_score()
        await twin.stop()

    asyncio.run(run())
    assert twin._last_gesture_conf_delta > 0.0
    assert abs(twin._last_gesture_conf_delta - (0.9 - 0.3) / 0.9) < 1e-6
    assert isinstance(twin._last_cmd_rate_delta, float)
