"""Tests for MemoryManager — Gap 2 (AIOS alignment).

Covers:
- Schema validation: invalid key/namespace pairs are rejected and logged
- Zero-copy pain-day hot-path accessors
- read_context fallback chain (semantic → db → empty list)
- write_state dispatches to the right AgentDB method
- get_status reflects current twin state
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.memory_manager import MemoryManager, _VALID_KEYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_memory(
    db=None,
    semantic=None,
    twin=None,
) -> MemoryManager:
    if db is None:
        db = MagicMock()
        db.available = True
    if semantic is None:
        semantic = MagicMock()
        semantic.available = False  # default: chromadb unavailable
    return MemoryManager(agent_db=db, semantic_memory=semantic, twin_state=twin)


def _make_twin(pain_day_active=False, pain_day_score=0.0):
    twin = MagicMock()
    twin._pain_day_active = pain_day_active
    twin._pain_day_score = pain_day_score
    return twin


# ---------------------------------------------------------------------------
# Schema validation table
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_valid_keys_table_not_empty(self):
        assert len(_VALID_KEYS) >= 3
        assert "accessibility" in _VALID_KEYS
        assert "dev_agent" in _VALID_KEYS

    def test_accessibility_has_few_shot_example(self):
        assert "few_shot_example" in _VALID_KEYS["accessibility"]

    def test_dev_agent_has_agent_step(self):
        assert "agent_step" in _VALID_KEYS["dev_agent"]

    def test_agent_run_not_in_accessibility(self):
        # agent_run belongs to dev_agent, not accessibility
        assert "agent_run" not in _VALID_KEYS["accessibility"]

    def test_few_shot_example_not_in_dev_agent(self):
        assert "few_shot_example" not in _VALID_KEYS["dev_agent"]


# ---------------------------------------------------------------------------
# _validate_write
# ---------------------------------------------------------------------------

class TestValidateWrite:
    def test_valid_accessibility_key_accepted(self):
        mm = _make_memory()
        assert mm._validate_write("few_shot_example", "accessibility") is True

    def test_valid_dev_agent_key_accepted(self):
        mm = _make_memory()
        assert mm._validate_write("agent_step", "dev_agent") is True

    def test_nonexistent_key_rejected(self):
        mm = _make_memory()
        assert mm._validate_write("nonexistent_key", "accessibility") is False

    def test_wrong_namespace_rejected(self):
        # agent_run is a dev_agent key, not an accessibility key
        mm = _make_memory()
        assert mm._validate_write("agent_run", "accessibility") is False

    def test_unknown_namespace_rejected(self):
        mm = _make_memory()
        assert mm._validate_write("few_shot_example", "bogus_namespace") is False


# ---------------------------------------------------------------------------
# write_state validation gating
# ---------------------------------------------------------------------------

class TestWriteStateValidation:
    @pytest.mark.asyncio
    async def test_invalid_key_logs_error_and_does_not_call_db(self, caplog):
        db = AsyncMock()
        mm = _make_memory(db=db)
        with caplog.at_level(logging.ERROR):
            await mm.write_state("nonexistent_key", ("a", "b"), "accessibility")
        assert "invalid write" in caplog.text.lower() or "dropped" in caplog.text.lower()
        db.upsert_few_shot_example.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_namespace_logs_error(self, caplog):
        db = AsyncMock()
        mm = _make_memory(db=db)
        with caplog.at_level(logging.ERROR):
            await mm.write_state("agent_run", {}, "accessibility")
        assert "invalid write" in caplog.text.lower() or "dropped" in caplog.text.lower()
        db.insert_agent_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_few_shot_dispatches_upsert(self):
        db = AsyncMock()
        db.upsert_few_shot_example = AsyncMock()
        mm = _make_memory(db=db)

        fake_cmd = MagicMock()
        await mm.write_state(
            "few_shot_example",
            (fake_cmd, "CLICK screen", "accessibility", 1),
            "accessibility",
        )
        db.upsert_few_shot_example.assert_called_once_with(
            fake_cmd, "CLICK screen", "accessibility", 1
        )

    @pytest.mark.asyncio
    async def test_valid_agent_step_dispatches_insert(self):
        db = AsyncMock()
        db.insert_agent_step = AsyncMock()
        mm = _make_memory(db=db)

        payload = {"run_id": 1, "step": "plan", "result": "ok"}
        await mm.write_state("agent_step", payload, "dev_agent")
        db.insert_agent_step.assert_called_once_with(**payload)

    @pytest.mark.asyncio
    async def test_valid_agent_run_dispatches_insert(self):
        db = AsyncMock()
        db.insert_agent_run = AsyncMock()
        mm = _make_memory(db=db)

        payload = {"session_id": 5, "goal": "fix bug", "status": "running"}
        await mm.write_state("agent_run", payload, "dev_agent")
        db.insert_agent_run.assert_called_once_with(**payload)


# ---------------------------------------------------------------------------
# Pain-day hot-path accessors (zero-copy, no await)
# ---------------------------------------------------------------------------

class TestPainDayAccessors:
    def test_returns_false_when_twin_none(self):
        mm = _make_memory(twin=None)
        assert mm.get_pain_day_active() is False

    def test_returns_zero_score_when_twin_none(self):
        mm = _make_memory(twin=None)
        assert mm.get_pain_day_score() == 0.0

    def test_reflects_twin_pain_day_active_false(self):
        twin = _make_twin(pain_day_active=False, pain_day_score=0.1)
        mm = _make_memory(twin=twin)
        assert mm.get_pain_day_active() is False

    def test_reflects_twin_pain_day_active_true(self):
        twin = _make_twin(pain_day_active=True, pain_day_score=0.8)
        mm = _make_memory(twin=twin)
        assert mm.get_pain_day_active() is True

    def test_reflects_twin_pain_day_score(self):
        twin = _make_twin(pain_day_active=True, pain_day_score=0.75)
        mm = _make_memory(twin=twin)
        assert abs(mm.get_pain_day_score() - 0.75) < 1e-9

    def test_active_true_immediately_after_set_manual_pain_day(self):
        """Critical: get_pain_day_active() must reflect set_manual_pain_day(True)
        without any await — it reads a plain attribute, not a DB value."""
        twin = MagicMock()
        twin._pain_day_active = False
        twin._pain_day_score = 0.0

        mm = _make_memory(twin=twin)
        assert mm.get_pain_day_active() is False

        # Simulate set_manual_pain_day(True) side-effect on the attribute
        twin._pain_day_active = True
        assert mm.get_pain_day_active() is True  # no await needed

    def test_set_twin_state_wires_new_twin(self):
        mm = _make_memory(twin=None)
        assert mm.get_pain_day_active() is False

        twin = _make_twin(pain_day_active=True, pain_day_score=0.9)
        mm.set_twin_state(twin)
        assert mm.get_pain_day_active() is True


# ---------------------------------------------------------------------------
# read_context fallback chain
# ---------------------------------------------------------------------------

class TestReadContext:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_both_unavailable(self):
        db = MagicMock()
        db.available = False
        semantic = MagicMock()
        semantic.available = False
        mm = MemoryManager(agent_db=db, semantic_memory=semantic)
        result = await mm.read_context("click the button")
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_semantic_when_available(self):
        semantic = MagicMock()
        semantic.available = True
        semantic.query_similar = AsyncMock(return_value=[{"text": "click", "action": "CLICK"}])
        db = MagicMock()
        db.available = True
        mm = MemoryManager(agent_db=db, semantic_memory=semantic)
        result = await mm.read_context("click something")
        assert result == [{"text": "click", "action": "CLICK"}]
        semantic.query_similar.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_db_when_semantic_unavailable(self):
        semantic = MagicMock()
        semantic.available = False
        db = MagicMock()
        db.available = True
        db.get_few_shot_examples = AsyncMock(return_value=[{"cmd": "scroll down"}])
        mm = MemoryManager(agent_db=db, semantic_memory=semantic)
        result = await mm.read_context("scroll", namespace="accessibility")
        assert result == [{"cmd": "scroll down"}]

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        semantic = MagicMock()
        semantic.available = True
        semantic.query_similar = AsyncMock(side_effect=RuntimeError("chromadb offline"))
        mm = MemoryManager(agent_db=None, semantic_memory=semantic)
        result = await mm.read_context("anything")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_semantic_delegates_to_read_context(self):
        mm = _make_memory()
        mm.read_context = AsyncMock(return_value=[{"text": "scroll"}])
        result = await mm.search_semantic("scroll down", n=3)
        mm.read_context.assert_called_once_with("scroll down", namespace="accessibility", n=3)
        assert result == [{"text": "scroll"}]


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestNewDispatchers:
    """Tranche 1 A2: previously-unwired _VALID_KEYS now dispatch correctly."""

    @pytest.mark.asyncio
    async def test_sensor_telemetry_dispatches_insert(self):
        db = AsyncMock()
        db.insert_sensor_telemetry = AsyncMock()
        mm = _make_memory(db=db)
        payload = {"session_id": 3, "ts": 1.0, "tilt_rx": 0.2, "pain_day_active": True}
        await mm.write_state("sensor_telemetry", payload, "system")
        db.insert_sensor_telemetry.assert_called_once_with(**payload)

    @pytest.mark.asyncio
    async def test_voice_profile_dispatches_upsert_with_single_dict(self):
        db = AsyncMock()
        db.upsert_voice_profile = AsyncMock()
        mm = _make_memory(db=db)
        payload = {"baseline_rms": 0.4, "vad_threshold": 0.015}
        await mm.write_state("voice_profile", payload, "system")
        # Passed positionally as a single dict, NOT **kwargs
        db.upsert_voice_profile.assert_called_once_with(payload)

    @pytest.mark.asyncio
    async def test_command_outcome_is_noop(self):
        """command_outcome validates (it's in _VALID_KEYS) but writes nothing —
        commands are inserted by HybridCoordinator, not here."""
        db = AsyncMock()
        mm = _make_memory(db=db)
        await mm.write_state("command_outcome", {"anything": 1}, "accessibility")
        assert db.mock_calls == []  # no DB method touched

    @pytest.mark.asyncio
    async def test_pain_day_score_passes_through_deltas(self):
        db = AsyncMock()
        db.log_pain_day = AsyncMock()
        mm = _make_memory(db=db)
        payload = {
            "session_id": 1, "score": 0.7, "active": True,
            "fail_ratio": 0.3, "clarify_ratio": 0.1,
            "gesture_conf_delta": 0.25, "cmd_rate_delta": 0.4,
        }
        await mm.write_state("pain_day_score", payload, "system")
        kwargs = db.log_pain_day.call_args.kwargs
        assert kwargs["gesture_conf_delta"] == 0.25
        assert kwargs["cmd_rate_delta"] == 0.4


class TestSessionEventRemoved:
    """Tranche 1 A2: session_event removed from _VALID_KEYS — now fails loudly."""

    def test_session_event_not_in_valid_keys(self):
        assert "session_event" not in _VALID_KEYS["system"]

    @pytest.mark.asyncio
    async def test_session_event_write_rejected(self, caplog):
        db = AsyncMock()
        mm = _make_memory(db=db)
        with caplog.at_level(logging.ERROR):
            await mm.write_state("session_event", {"x": 1}, "system")
        assert "invalid write" in caplog.text.lower() or "dropped" in caplog.text.lower()
        assert db.mock_calls == []  # not silently dropped into a debug log either


class TestReadContextNamespace:
    """Tranche 1 A3: namespace honored consistently — non-accessibility reads
    route to AgentDB (domain-filtered), never the namespace-blind ChromaDB."""

    @pytest.mark.asyncio
    async def test_dev_agent_namespace_skips_chromadb(self):
        semantic = MagicMock()
        semantic.available = True
        semantic.query_similar = AsyncMock(return_value=[{"text": "should not be used"}])
        db = MagicMock()
        db.available = True
        db.get_few_shot_examples = AsyncMock(return_value=[{"text": "dev hit"}])
        mm = MemoryManager(agent_db=db, semantic_memory=semantic)

        result = await mm.read_context("build the plan", namespace="dev_agent")

        semantic.query_similar.assert_not_called()
        db.get_few_shot_examples.assert_called_once()
        assert db.get_few_shot_examples.call_args.kwargs["domain"] == "dev_agent"
        assert result == [{"text": "dev hit"}]

    @pytest.mark.asyncio
    async def test_accessibility_namespace_uses_chromadb(self):
        semantic = MagicMock()
        semantic.available = True
        semantic.query_similar = AsyncMock(return_value=[{"text": "acc hit"}])
        db = MagicMock()
        db.available = True
        db.get_few_shot_examples = AsyncMock(return_value=[{"text": "db"}])
        mm = MemoryManager(agent_db=db, semantic_memory=semantic)

        result = await mm.read_context("click", namespace="accessibility")

        semantic.query_similar.assert_called_once()
        db.get_few_shot_examples.assert_not_called()
        assert result == [{"text": "acc hit"}]


class TestGetStatus:
    def test_returns_dict_with_expected_keys(self):
        mm = _make_memory()
        status = mm.get_status()
        assert "db_available" in status
        assert "semantic_available" in status
        assert "pain_day_active" in status
        assert "pain_day_score" in status

    def test_pain_day_active_reflects_twin(self):
        twin = _make_twin(pain_day_active=True, pain_day_score=0.65)
        mm = _make_memory(twin=twin)
        status = mm.get_status()
        assert status["pain_day_active"] is True
        assert abs(status["pain_day_score"] - 0.65) < 0.01

    def test_no_twin_gives_safe_defaults(self):
        mm = _make_memory(twin=None)
        status = mm.get_status()
        assert status["pain_day_active"] is False
        assert status["pain_day_score"] == 0.0
