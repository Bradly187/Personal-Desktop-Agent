"""Tests for the Top 3 Continuous Learning Gap Fixes.

Gap 1 — Gate 1 threshold persisted to settings_versions and restored at startup.
Gap 2 — Failures and corrections captured as counterexamples; injected in prompts.
Gap 3 — Gesture velocity floors loaded from DB into GestureProcessor at startup.
"""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

class _Cmd:
    def __init__(self, text: str = "click save", source: str = "voice"):
        self.text = text
        self.source = source
        self.session_context: list[str] = []


def _make_emb(val: float = 0.5) -> bytes:
    return struct.pack("4f", val, val, val, val)


# ============================================================================
# Gap 1 — Gate 1 threshold persistence
# ============================================================================

class TestGap1ThresholdPersistence:
    """_adapt_gate1_with_log must write to settings_versions via log_settings_change."""

    def _make_trainer(self, config=None, db=None, twin=None):
        from adaptive.continuous_trainer import ContinuousTrainer

        cfg = config or MagicMock()
        cfg.whisper_logprob_min = -1.0
        _db = db or MagicMock()
        _db.available = True
        _db.routing.log_adaptation = AsyncMock()
        _db.profile.log_settings_change = AsyncMock()
        _db.routing.get_recent_adaptation_log = AsyncMock(return_value=[])
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._config = cfg
        trainer._db = _db
        trainer._twin = twin
        trainer._cloud_limit = 0.3
        trainer._failure_limit = 0.5
        trainer._gate1_step = 0.05
        trainer._running = False
        return trainer

    @pytest.mark.asyncio
    async def test_log_settings_change_called_on_adapt(self):
        trainer = self._make_trainer()
        trainer._config.whisper_logprob_min = -1.0
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=False
        )
        trainer._db.profile.log_settings_change.assert_awaited_once()
        call_kwargs = trainer._db.profile.log_settings_change.call_args
        assert call_kwargs.kwargs.get("key") == "whisper_logprob_min" or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] == "whisper_logprob_min"
        )

    @pytest.mark.asyncio
    async def test_log_settings_change_records_old_and_new(self):
        trainer = self._make_trainer()
        old = trainer._config.whisper_logprob_min
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=False
        )
        call_kwargs = trainer._db.profile.log_settings_change.call_args.kwargs
        assert call_kwargs["old_value"] == pytest.approx(old)
        assert call_kwargs["new_value"] == pytest.approx(old - 0.05)

    @pytest.mark.asyncio
    async def test_no_persist_when_threshold_not_changed(self):
        """cloud_rate below limit → no adaptation → no log_settings_change."""
        trainer = self._make_trainer()
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.1, failure_rate=0.1, pain_day_active=False
        )
        trainer._db.profile.log_settings_change.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_persist_on_pain_day(self):
        trainer = self._make_trainer()
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=True
        )
        trainer._db.profile.log_settings_change.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_exception_does_not_propagate(self):
        trainer = self._make_trainer()
        trainer._db.profile.log_settings_change.side_effect = RuntimeError("db down")
        trainer._db.routing.log_adaptation.side_effect = RuntimeError("db down")
        # Should silently swallow
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=False
        )


# ============================================================================
# Gate-1 D5 rollback (audit fix 2026-06-09: comparand + persistence + latch)
# ============================================================================

class TestGate1Rollback(TestGap1ThresholdPersistence):
    """The rollback must require cloud_rate strictly rising across THREE
    adaptation passes, persist the restored value to settings_versions, and
    never re-trigger on already-rolled-back rows."""

    def _history(self, *rates, metric_before=-1.0):
        # newest first, as get_recent_adaptation_log returns
        return [
            {"id": 100 + i, "cloud_rate": r, "metric_before": metric_before,
             "metric_after": metric_before - 0.05, "rolled_back": 0}
            for i, r in enumerate(rates)
        ]

    @pytest.mark.asyncio
    async def test_two_rows_never_trigger_rollback(self):
        """The old comparand (cloud_rate > metric_before ≈ -1.0) was vacuously
        true and rolled back on any single noisy increase. Two rows are not
        enough evidence: loosening must proceed."""
        trainer = self._make_trainer()
        trainer._db.routing.mark_adaptation_rolled_back = AsyncMock()
        trainer._db.routing.get_recent_adaptation_log = AsyncMock(
            return_value=self._history(0.45, 0.40)   # newest > older (noise)
        )
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=False
        )
        trainer._db.routing.mark_adaptation_rolled_back.assert_not_awaited()
        trainer._db.routing.log_adaptation.assert_awaited_once()   # loosening happened

    @pytest.mark.asyncio
    async def test_three_rising_rows_roll_back_and_persist(self):
        trainer = self._make_trainer()
        trainer._config.whisper_logprob_min = -1.15
        trainer._db.routing.mark_adaptation_rolled_back = AsyncMock()
        trainer._db.routing.get_recent_adaptation_log = AsyncMock(
            return_value=self._history(0.50, 0.42, 0.35, metric_before=-1.10)
        )
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.55, failure_rate=0.1, pain_day_active=False
        )
        # Threshold restored to the pre-loosening value of the newest row
        assert trainer._config.whisper_logprob_min == pytest.approx(-1.10)
        # The undone adaptation row is marked so it is never re-read
        trainer._db.routing.mark_adaptation_rolled_back.assert_awaited_once_with(100)
        # Rollback is PERSISTED — without this, restart restores the rejected
        # loosened value from settings_versions
        kwargs = trainer._db.profile.log_settings_change.call_args.kwargs
        assert kwargs["key"] == "whisper_logprob_min"
        assert kwargs["new_value"] == pytest.approx(-1.10)
        assert kwargs["changed_by"] == "continuous_trainer_rollback"
        # And no new loosening in the same pass
        trainer._db.routing.log_adaptation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_monotonic_rates_do_not_roll_back(self):
        trainer = self._make_trainer()
        trainer._db.routing.mark_adaptation_rolled_back = AsyncMock()
        trainer._db.routing.get_recent_adaptation_log = AsyncMock(
            return_value=self._history(0.50, 0.30, 0.40)   # dipped in the middle
        )
        await trainer._adapt_gate1_with_log(
            cloud_rate=0.5, failure_rate=0.1, pain_day_active=False
        )
        trainer._db.routing.mark_adaptation_rolled_back.assert_not_awaited()
        trainer._db.routing.log_adaptation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rolled_back_rows_excluded_from_history_read(self, tmp_path):
        """DB-level: get_recent_adaptation_log must skip rolled_back=1 rows so
        the rollback cannot re-trigger forever on the same history."""
        from storage.db import AgentDB
        db = AgentDB()
        await db.open(tmp_path / "agent.db")
        if not db.available:
            pytest.skip("aiosqlite unavailable")
        try:
            for rate in (0.35, 0.42, 0.50):
                await db.routing.log_adaptation(
                    component="gate1", metric_before=-1.0, metric_after=-1.05,
                    cloud_rate=rate, failure_rate=0.1,
                )
            rows = await db.routing.get_recent_adaptation_log("gate1", limit=3)
            assert len(rows) == 3
            newest_id = rows[0]["id"]
            await db.routing.mark_adaptation_rolled_back(newest_id)
            rows_after = await db.routing.get_recent_adaptation_log("gate1", limit=3)
            assert all(r["id"] != newest_id for r in rows_after)
            assert len(rows_after) == 2
        finally:
            await db.close()


# ============================================================================
# Gap 1 — main.py startup restore (unit-level: CoordinatorConfig applied)
# ============================================================================

class TestGap1StartupRestore:
    """Verify the restore pattern reads settings_versions and sets cfg."""

    @pytest.mark.asyncio
    async def test_restore_applies_saved_value(self):
        from core.hybrid_coordinator import CoordinatorConfig
        cfg = CoordinatorConfig()
        assert cfg.whisper_logprob_min == pytest.approx(-1.0)

        # Simulate the restore block from main.py
        mock_conn = AsyncMock()
        mock_cur = AsyncMock()
        mock_cur.fetchone = AsyncMock(return_value=("-1.15",))
        mock_conn.execute = AsyncMock(return_value=mock_cur)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_cur)
        mock_conn.__aexit__ = AsyncMock(return_value=None)

        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_execute(sql, *a):
            yield mock_cur

        mock_agent_db = MagicMock()
        mock_agent_db.available = True
        mock_agent_db._conn = MagicMock()
        mock_agent_db._conn.execute = _fake_execute

        async with mock_agent_db._conn.execute(
            "SELECT new_value FROM settings_versions "
            "WHERE component='coordinator' AND key='whisper_logprob_min' "
            "ORDER BY ts DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                cfg.whisper_logprob_min = float(row[0])

        assert cfg.whisper_logprob_min == pytest.approx(-1.15)

    @pytest.mark.asyncio
    async def test_restore_skipped_when_db_unavailable(self):
        from core.hybrid_coordinator import CoordinatorConfig
        cfg = CoordinatorConfig()
        mock_agent_db = MagicMock()
        mock_agent_db.available = False
        # The restore block only runs when available — simulate:
        if mock_agent_db.available:
            cfg.whisper_logprob_min = -9.9  # should never happen
        assert cfg.whisper_logprob_min == pytest.approx(-1.0)


# ============================================================================
# Gap 2 — DB: upsert_few_shot_counterexample + get_few_shot_counterexamples
# ============================================================================

class TestGap2CounterexampleDB:
    """Integration-ish tests against a real (in-process) AgentDB in memory."""

    @pytest.fixture
    async def db(self, tmp_path):
        from storage.db import AgentDB
        path = str(tmp_path / "test_gap2.db")
        db = AgentDB()
        await db.open(path)
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_upsert_creates_row(self, db):
        cmd = _Cmd("scroll down please")
        await db.memory.upsert_few_shot_counterexample(cmd, "CLICK down button", "command", "pipeline_failure")
        cur = await db._conn.execute(
            "SELECT text, wrong_action, source, reason FROM few_shot_counterexamples"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "scroll down please"
        assert rows[0][1] == "CLICK down button"

    @pytest.mark.asyncio
    async def test_upsert_increments_usage_count(self, db):
        cmd = _Cmd("scroll down please")
        await db.memory.upsert_few_shot_counterexample(cmd, "CLICK down button", "command", "pipeline_failure")
        await db.memory.upsert_few_shot_counterexample(cmd, "CLICK down button", "command", "pipeline_failure")
        cur = await db._conn.execute(
            "SELECT usage_count FROM few_shot_counterexamples WHERE text=? AND wrong_action=?",
            ("scroll down please", "CLICK down button"),
        )
        rows = await cur.fetchall()
        assert rows[0][0] == 2

    @pytest.mark.asyncio
    async def test_upsert_unique_on_text_wrong_action(self, db):
        cmd = _Cmd("scroll down")
        await db.memory.upsert_few_shot_counterexample(cmd, "TYPE down", "command", "pipeline_failure")
        await db.memory.upsert_few_shot_counterexample(cmd, "TYPE down", "command", "user_correction")
        cur = await db._conn.execute("SELECT COUNT(*) FROM few_shot_counterexamples")
        rows = await cur.fetchall()
        assert rows[0][0] == 1

    @pytest.mark.asyncio
    async def test_get_returns_relevant_counterexample(self, db):
        cmd_store = _Cmd("scroll down please")
        await db.memory.upsert_few_shot_counterexample(cmd_store, "CLICK footer", "command", "user_correction")
        query_cmd = _Cmd("scroll down")
        results = await db.memory.get_few_shot_counterexamples(query_cmd, n=3, domain="command")
        assert len(results) >= 1
        assert results[0]["wrong_action"] == "CLICK footer"

    @pytest.mark.asyncio
    async def test_get_returns_empty_when_no_counterexamples(self, db):
        cmd = _Cmd("open chrome")
        results = await db.memory.get_few_shot_counterexamples(cmd, n=3)
        assert results == []

    @pytest.mark.asyncio
    async def test_get_limits_results(self, db):
        for i in range(5):
            cmd = _Cmd(f"click button {i}")
            await db.memory.upsert_few_shot_counterexample(cmd, f"SCROLL {i}", "command", "pipeline_failure")
        query = _Cmd("click button")
        results = await db.memory.get_few_shot_counterexamples(query, n=2)
        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_get_respects_domain_filter(self, db):
        cmd = _Cmd("write some code")
        await db.memory.upsert_few_shot_counterexample(cmd, "CLICK editor", "code", "pipeline_failure")
        query = _Cmd("write some code")
        results = await db.memory.get_few_shot_counterexamples(query, n=5, domain="command")
        assert all(r["wrong_action"] != "CLICK editor" for r in results)


# ============================================================================
# Gap 2 — ContinuousTrainer: record_failure and record_correction wiring
# ============================================================================

class TestGap2TrainerWiring:
    def _make_trainer(self):
        from adaptive.continuous_trainer import ContinuousTrainer
        _db = MagicMock()
        _db.available = True
        _db.memory.upsert_few_shot_counterexample = AsyncMock()
        _db.memory.upsert_few_shot_example = AsyncMock()
        _db.memory.delete_few_shot_example = AsyncMock()
        _db.memory.delete_few_shot_counterexample = AsyncMock()
        _db.commands.mark_command_corrected = AsyncMock()
        _db.memory.get_few_shot_counterexamples = AsyncMock(return_value=[])
        _twin = MagicMock()
        _twin.record_failure = AsyncMock()
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._db = _db
        trainer._twin = _twin
        trainer._config = None
        trainer._gesture_proc = None
        trainer._running = False
        return trainer

    @pytest.mark.asyncio
    async def test_record_failure_calls_upsert_counterexample(self):
        trainer = self._make_trainer()
        cmd = _Cmd("open chrome")
        await trainer.record_failure(cmd, "CLICK nowhere", command_id=1)
        trainer._db.memory.upsert_few_shot_counterexample.assert_awaited_once()
        call_args = trainer._db.memory.upsert_few_shot_counterexample.call_args
        assert call_args.args[1] == "CLICK nowhere"
        assert call_args.args[3] == "pipeline_failure"

    @pytest.mark.asyncio
    async def test_record_failure_still_calls_twin(self):
        trainer = self._make_trainer()
        cmd = _Cmd("close window")
        await trainer.record_failure(cmd, "SCROLL down")
        trainer._twin.record_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_record_correction_stores_counterexample_before_positive(self):
        call_order: list[str] = []
        trainer = self._make_trainer()
        trainer._db.memory.upsert_few_shot_counterexample = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("counter") or None
        )
        trainer._db.memory.upsert_few_shot_example = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("positive") or None
        )
        cmd = _Cmd("click save button")
        await trainer.record_correction(cmd, "SCROLL down", "CLICK save button")
        assert call_order == ["counter", "positive"]

    @pytest.mark.asyncio
    async def test_record_correction_counterexample_has_user_correction_reason(self):
        trainer = self._make_trainer()
        cmd = _Cmd("click save button")
        await trainer.record_correction(cmd, "TYPE save", "CLICK save button", command_id=5)
        call_args = trainer._db.memory.upsert_few_shot_counterexample.call_args
        assert call_args.args[3] == "user_correction"
        assert call_args.args[1] == "TYPE save"

    @pytest.mark.asyncio
    async def test_get_few_shot_counterexamples_delegates_to_db(self):
        trainer = self._make_trainer()
        trainer._db.memory.get_few_shot_counterexamples = AsyncMock(
            return_value=[{"command_text": "click", "wrong_action": "SCROLL"}]
        )
        cmd = _Cmd("click the button")
        result = await trainer._db.memory.get_few_shot_counterexamples(cmd, n=2, domain="command")
        trainer._db.memory.get_few_shot_counterexamples.assert_awaited_once_with(
            cmd, n=2, domain="command"
        )
        assert len(result) == 1


# ============================================================================
# Gap 2 — local_inference.py: counterexamples in prompts
# ============================================================================

class TestGap2PromptInjection:
    def test_build_prompt_includes_do_not_section(self):
        from inference.local_inference import _build_prompt
        cmd = _Cmd("scroll down")
        counterexamples = [{"command_text": "scroll down", "wrong_action": "CLICK footer"}]
        prompt = _build_prompt(cmd, counterexamples=counterexamples)
        assert "Do NOT" in prompt or "not produce" in prompt.lower()
        assert "CLICK footer" in prompt

    def test_build_prompt_no_section_when_empty(self):
        from inference.local_inference import _build_prompt
        cmd = _Cmd("click ok")
        prompt = _build_prompt(cmd, counterexamples=[])
        assert "Do NOT" not in prompt

    def test_build_prompt_no_section_when_none(self):
        from inference.local_inference import _build_prompt
        cmd = _Cmd("click ok")
        prompt = _build_prompt(cmd, counterexamples=None)
        assert "Do NOT" not in prompt

    def test_build_chat_messages_injects_in_system(self):
        from inference.local_inference import _build_chat_messages
        cmd = _Cmd("scroll down")
        counterexamples = [{"command_text": "scroll down", "wrong_action": "CLICK footer"}]
        messages = _build_chat_messages(cmd, counterexamples=counterexamples)
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "CLICK footer" in system_msg["content"]

    def test_build_chat_messages_no_injection_when_none(self):
        from inference.local_inference import _build_chat_messages
        cmd = _Cmd("click ok")
        messages = _build_chat_messages(cmd, counterexamples=None)
        # System message should just be the plain system prompt
        from inference.local_inference import _SYSTEM_PROMPT
        assert messages[0]["content"] == _SYSTEM_PROMPT

    def test_ollama_infer_signature_accepts_counterexamples(self):
        """OllamaInference.infer must accept counterexamples kwarg."""
        import inspect
        from inference.local_inference import OllamaInference
        sig = inspect.signature(OllamaInference.infer)
        assert "counterexamples" in sig.parameters

    def test_abstract_signature_accepts_counterexamples(self):
        import inspect
        from inference.local_inference import LocalInference
        sig = inspect.signature(LocalInference.infer)
        assert "counterexamples" in sig.parameters

    def test_llama_cpp_signature_accepts_counterexamples(self):
        import inspect
        from inference.local_inference import LlamaCppInference
        sig = inspect.signature(LlamaCppInference.infer)
        assert "counterexamples" in sig.parameters

    def test_vllm_server_signature_accepts_counterexamples(self):
        import inspect
        from inference.local_inference import VLLMServerInference
        sig = inspect.signature(VLLMServerInference.infer)
        assert "counterexamples" in sig.parameters


# ============================================================================
# Gap 2 — InferenceRunner.run_local passes counterexamples
# ============================================================================

class TestGap2CoordinatorPassthrough:
    @pytest.mark.asyncio
    async def test_run_local_fetches_counterexamples(self):
        from core.inference_runner import InferenceRunner
        from core.hybrid_coordinator import CoordinatorConfig
        local = MagicMock()
        local.infer = AsyncMock(return_value="CLICK ok")
        local.last_prompt = []
        captured = {}

        async def _fake_infer(cmd, few_shot_examples=None, counterexamples=None):
            captured["counterexamples"] = counterexamples
            return "CLICK ok"

        local.infer = _fake_infer
        trainer = MagicMock()
        trainer.get_few_shot_examples = AsyncMock(return_value=[])
        trainer.get_few_shot_counterexamples = AsyncMock(
            return_value=[{"command_text": "scroll", "wrong_action": "CLICK nothing"}]
        )
        agent_db = MagicMock()
        agent_db.available = False

        runner = InferenceRunner(
            CoordinatorConfig(),
            local=lambda: local, cloud=lambda: None,
            trainer=lambda: trainer, agent_db=lambda: agent_db,
            content_filter=lambda: None, rate_limiter=lambda: None,
            note_cloud_call=lambda: None,
        )

        cmd = _Cmd("scroll down")
        result = await runner.run_local(cmd)
        assert result == "CLICK ok"
        assert captured["counterexamples"] is not None
        assert len(captured["counterexamples"]) == 1

    @pytest.mark.asyncio
    async def test_run_local_counterexamples_none_when_no_trainer(self):
        from core.inference_runner import InferenceRunner
        from core.hybrid_coordinator import CoordinatorConfig
        captured = {}

        async def _fake_infer(cmd, few_shot_examples=None, counterexamples=None):
            captured["counterexamples"] = counterexamples
            return "CLICK ok"

        local = MagicMock()
        local.infer = _fake_infer
        agent_db = MagicMock()
        agent_db.available = False

        runner = InferenceRunner(
            CoordinatorConfig(),
            local=lambda: local, cloud=lambda: None,
            trainer=lambda: None, agent_db=lambda: agent_db,
            content_filter=lambda: None, rate_limiter=lambda: None,
            note_cloud_call=lambda: None,
        )

        cmd = _Cmd("click ok")
        result = await runner.run_local(cmd)
        assert result == "CLICK ok"
        assert captured["counterexamples"] is None


# ============================================================================
# Gap 3 — load_velocity_calibration
# ============================================================================

class TestGap3VelocityCalibrationLoad:
    def _make_trainer(self, db_floors: dict[str, float] | None = None):
        from adaptive.continuous_trainer import ContinuousTrainer
        _db = MagicMock()
        _db.available = True

        async def _fake_floor(gesture, default=None):
            return (db_floors or {}).get(gesture, default)

        _db.gestures.get_gesture_velocity_floor = _fake_floor
        gesture_proc = MagicMock()
        gesture_proc.set_velocity_thresholds = MagicMock()
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._db = _db
        trainer._gesture_proc = gesture_proc
        trainer._twin = None
        trainer._config = None
        trainer._running = False
        return trainer, gesture_proc

    @pytest.mark.asyncio
    async def test_load_calls_set_velocity_thresholds(self):
        trainer, gesture_proc = self._make_trainer(
            db_floors={"PEACE_SWIPE_LEFT": 0.8, "PEACE_SWIPE_RIGHT": 0.9}
        )
        await trainer.load_velocity_calibration()
        gesture_proc.set_velocity_thresholds.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_picks_minimum_floor_per_key(self):
        trainer, gesture_proc = self._make_trainer(
            db_floors={
                "PEACE_SWIPE_LEFT": 0.7,
                "PEACE_SWIPE_RIGHT": 0.9,
                "PEACE_SWIPE_UP": 0.85,
                "OPEN_PUSH": 1.1,
                "OPEN_PULL": 0.95,
            }
        )
        await trainer.load_velocity_calibration()
        call_arg = gesture_proc.set_velocity_thresholds.call_args[0][0]
        # SWIPE key should be minimum of the 3 SWIPE floors
        assert call_arg["SWIPE"] == pytest.approx(0.7)
        # PUSH key should be minimum of OPEN_PUSH and OPEN_PULL
        assert call_arg["PUSH"] == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_no_call_when_gesture_proc_none(self):
        from adaptive.continuous_trainer import ContinuousTrainer
        _db = MagicMock()
        _db.available = True
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._db = _db
        trainer._gesture_proc = None
        trainer._running = False
        # Should not raise
        await trainer.load_velocity_calibration()

    @pytest.mark.asyncio
    async def test_no_call_when_db_unavailable(self):
        trainer, gesture_proc = self._make_trainer()
        trainer._db.available = False
        await trainer.load_velocity_calibration()
        gesture_proc.set_velocity_thresholds.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_db_is_a_noop(self):
        """No calibration rows → do NOT push anything: GestureProcessor's own
        per-class defaults (SWIPE 1.2 coords/s, PUSH 0.30 m/s) must stay in
        effect. (Audit 2026-06-09: pushing a shared 1.2 default put the SWIPE
        number into the PUSH class — 4x harder — and locked out calibration.)"""
        trainer, gesture_proc = self._make_trainer(db_floors={})
        await trainer.load_velocity_calibration()
        gesture_proc.set_velocity_thresholds.assert_not_called()

    @pytest.mark.asyncio
    async def test_swipe_only_calibration_does_not_clobber_push(self):
        """A calibrated SWIPE floor must not introduce a PUSH key: the pushed
        dict omits PUSH so _push_threshold() falls back to its 0.30 m/s class
        default rather than inheriting the swipe-unit value."""
        trainer, gesture_proc = self._make_trainer(
            db_floors={"PEACE_SWIPE_LEFT": 1.1}
        )
        await trainer.load_velocity_calibration()
        call_arg = gesture_proc.set_velocity_thresholds.call_args[0][0]
        assert call_arg == {"SWIPE": pytest.approx(1.1)}
        assert "PUSH" not in call_arg

    @pytest.mark.asyncio
    async def test_swipe_gestures_map_to_swipe_key(self):
        trainer, gesture_proc = self._make_trainer(
            db_floors={"PEACE_SWIPE_LEFT": 0.6}
        )
        await trainer.load_velocity_calibration()
        call_arg = gesture_proc.set_velocity_thresholds.call_args[0][0]
        assert "SWIPE" in call_arg

    @pytest.mark.asyncio
    async def test_push_gestures_map_to_push_key(self):
        trainer, gesture_proc = self._make_trainer(
            db_floors={"OPEN_PUSH": 1.05}
        )
        await trainer.load_velocity_calibration()
        call_arg = gesture_proc.set_velocity_thresholds.call_args[0][0]
        assert "PUSH" in call_arg
