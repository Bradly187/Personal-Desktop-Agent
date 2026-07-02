"""Tests for the two post-audit continuous-learning fixes.

Fix 1 — counterexample poisoning containment:
  An execution failure is not proof the LLM mapping was wrong, so
  pipeline_failure counterexamples must clear guards before reaching a prompt:
  usage_count >= 2, no contradicting positive example, similarity floor.
  user_correction rows inject immediately; a later success retires the pair;
  a user correction retires the stale positive example for the wrong pair.

Fix 2 — prompt-attribution race:
  (prompt, tokens_in, tokens_out) capture moved from backend instance
  attributes to a task-local ContextVar so concurrent route() tasks cannot
  misattribute prompts/token counts across overlapping inferences.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _Cmd:
    def __init__(self, text: str = "click save", source: str = "voice"):
        self.text = text
        self.source = source
        self.session_context: list[str] = []


@pytest.fixture
async def db(tmp_path):
    from storage.db import AgentDB
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


@pytest.fixture(autouse=True)
def _force_jaccard(monkeypatch):
    """Disable the MiniLM encoder for every test in this module: similarity
    becomes deterministic word overlap (Jaccard) and no model load happens."""
    import storage.db as db_mod

    async def _no_encoder():
        return None

    monkeypatch.setattr(db_mod, "_get_encoder", _no_encoder)


# ============================================================================
# Fix 1 — injection guards in get_few_shot_counterexamples
# ============================================================================

class TestPipelineFailureUsageGate:
    @pytest.mark.asyncio
    async def test_single_pipeline_failure_not_injected(self, db):
        """One transient execution failure must never reach a prompt."""
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        results = await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)
        assert results == []

    @pytest.mark.asyncio
    async def test_repeated_pipeline_failure_injected(self, db):
        """The same pair failing twice is real evidence — inject it."""
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        results = await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)
        assert len(results) == 1
        assert results[0]["wrong_action"] == "OPEN Chrome"

    @pytest.mark.asyncio
    async def test_user_correction_injected_immediately(self, db):
        """A user correction is direct evidence — no usage_count gate."""
        cmd = _Cmd("scroll down")
        await db.upsert_few_shot_counterexample(cmd, "CLICK footer", "command", "user_correction")
        results = await db.get_few_shot_counterexamples(_Cmd("scroll down"), n=3)
        assert len(results) == 1
        assert results[0]["wrong_action"] == "CLICK footer"


class TestContradictionGuard:
    @pytest.mark.asyncio
    async def test_pair_with_positive_example_not_injected(self, db):
        """A pair that exists as a positive few-shot example must not also
        appear as a counterexample in the same prompt."""
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_example(cmd, "OPEN Chrome", "command")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        results = await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)
        assert results == []

    @pytest.mark.asyncio
    async def test_different_action_still_injected(self, db):
        """The positive example only suppresses the SAME (text, action) pair."""
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_example(cmd, "OPEN Chrome", "command")
        await db.upsert_few_shot_counterexample(cmd, "CLICK chrome icon", "command", "user_correction")
        results = await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)
        assert len(results) == 1
        assert results[0]["wrong_action"] == "CLICK chrome icon"


class TestSimilarityFloor:
    @pytest.mark.asyncio
    async def test_unrelated_counterexample_not_injected(self, db):
        """Zero word overlap (Jaccard fallback) stays out of the prompt."""
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Firefox", "command", "user_correction")
        results = await db.get_few_shot_counterexamples(_Cmd("scroll down"), n=3)
        assert results == []


class TestReasonUpgrade:
    @pytest.mark.asyncio
    async def test_reason_upgrades_to_user_correction(self, db):
        cmd = _Cmd("scroll down")
        await db.upsert_few_shot_counterexample(cmd, "CLICK footer", "command", "pipeline_failure")
        await db.upsert_few_shot_counterexample(cmd, "CLICK footer", "command", "user_correction")
        async with db._conn.execute(
            "SELECT reason FROM few_shot_counterexamples WHERE text=? AND wrong_action=?",
            ("scroll down", "CLICK footer"),
        ) as cur:
            row = await cur.fetchone()
        assert row["reason"] == "user_correction"

    @pytest.mark.asyncio
    async def test_reason_never_downgrades(self, db):
        cmd = _Cmd("scroll down")
        await db.upsert_few_shot_counterexample(cmd, "CLICK footer", "command", "user_correction")
        await db.upsert_few_shot_counterexample(cmd, "CLICK footer", "command", "pipeline_failure")
        async with db._conn.execute(
            "SELECT reason FROM few_shot_counterexamples WHERE text=? AND wrong_action=?",
            ("scroll down", "CLICK footer"),
        ) as cur:
            row = await cur.fetchone()
        assert row["reason"] == "user_correction"


# ============================================================================
# Fix 1 — success retires the counterexample / correction retires the positive
# ============================================================================

class TestSupersession:
    @pytest.mark.asyncio
    async def test_delete_counterexample_removes_row(self, db):
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_counterexample(cmd, "OPEN Chrome", "command", "pipeline_failure")
        await db.delete_few_shot_counterexample("open chrome", "OPEN Chrome")
        async with db._conn.execute("SELECT COUNT(*) FROM few_shot_counterexamples") as cur:
            row = await cur.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_delete_counterexample_noop_when_absent(self, db):
        # Must not raise.
        await db.delete_few_shot_counterexample("never seen", "CLICK nothing")

    @pytest.mark.asyncio
    async def test_delete_positive_example_removes_row(self, db):
        cmd = _Cmd("open chrome")
        await db.upsert_few_shot_example(cmd, "OPEN Chrome", "command")
        await db.delete_few_shot_example("open chrome", "OPEN Chrome")
        async with db._conn.execute("SELECT COUNT(*) FROM few_shot_examples") as cur:
            row = await cur.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_record_success_retires_counterexample(self, db):
        """Trainer-level: fail twice (injectable), then succeed — gone."""
        from adaptive.continuous_trainer import ContinuousTrainer
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._db = db
        trainer._twin = None
        trainer._memory = None
        trainer._gesture_proc = None
        trainer._running = False

        cmd = _Cmd("open chrome")
        await trainer.record_failure(cmd, "OPEN Chrome")
        await trainer.record_failure(cmd, "OPEN Chrome")
        assert await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)

        await trainer.record_success(cmd, "OPEN Chrome")
        assert await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3) == []

    @pytest.mark.asyncio
    async def test_record_correction_retires_stale_positive(self, db):
        """Trainer-level: a success-recorded pair the user later rejects must
        stop being a positive example AND start injecting as a counterexample."""
        from adaptive.continuous_trainer import ContinuousTrainer
        trainer = ContinuousTrainer.__new__(ContinuousTrainer)
        trainer._db = db
        trainer._twin = None
        trainer._memory = None
        trainer._gesture_proc = None
        trainer._running = False

        cmd = _Cmd("open chrome")
        await trainer.record_success(cmd, "OPEN Chromium")          # stale positive
        await trainer.record_correction(cmd, "OPEN Chromium", "OPEN Chrome")

        # The rejected pair is no longer a positive example…
        async with db._conn.execute(
            "SELECT COUNT(*) FROM few_shot_examples WHERE text=? AND action=?",
            ("open chrome", "OPEN Chromium"),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 0
        # …and the correction's counterexample injects (not suppressed by it).
        results = await db.get_few_shot_counterexamples(_Cmd("open chrome"), n=3)
        assert any(r["wrong_action"] == "OPEN Chromium" for r in results)


# ============================================================================
# Fix 2 — task-local inference capture (race regression)
# ============================================================================

class TestInferenceCaptureContextVar:
    @pytest.mark.asyncio
    async def test_concurrent_tasks_do_not_share_capture(self):
        from inference.local_inference import get_inference_capture, set_inference_capture

        async def _task(prompt: str, delay: float):
            set_inference_capture(prompt, 1, 2)
            await asyncio.sleep(delay)   # let the other task set its own value
            return get_inference_capture()[0]

        a, b = await asyncio.gather(_task("prompt-A", 0.02), _task("prompt-B", 0.01))
        assert a == "prompt-A"
        assert b == "prompt-B"

    @pytest.mark.asyncio
    async def test_capture_defaults_to_none(self):
        from inference.local_inference import get_inference_capture
        assert get_inference_capture() == (None, None, None)


def _make_runner(infer):
    from core.inference_runner import InferenceRunner
    from core.hybrid_coordinator import CoordinatorConfig
    local = MagicMock()
    local.infer = infer
    local.get_status = MagicMock(
        return_value={"model": "llama3.1:8b", "backend": "ollama"}
    )
    agent_db = MagicMock()
    agent_db.available = True
    agent_db.insert_inference = AsyncMock(return_value=1)
    runner = InferenceRunner(
        CoordinatorConfig(),
        local=lambda: local, cloud=lambda: None,
        trainer=lambda: None, agent_db=lambda: agent_db,
        content_filter=lambda: None, rate_limiter=lambda: None,
        note_cloud_call=lambda: None,
    )
    runner._agent_db_mock = agent_db  # test-only handle for assertions
    return runner


class TestRunLocalCapture:
    @pytest.mark.asyncio
    async def test_insert_uses_capture_set_by_infer(self):
        from inference.local_inference import set_inference_capture

        async def _infer(cmd, few_shot_examples=None, counterexamples=None):
            set_inference_capture("captured-prompt", 11, 3)
            return "CLICK ok"

        runner = _make_runner(_infer)
        await runner.run_local(_Cmd("click ok"))
        kwargs = runner._agent_db_mock.insert_inference.call_args.kwargs
        assert kwargs["prompt"] == "captured-prompt"
        assert kwargs["tokens_in"] == 11
        assert kwargs["tokens_out"] == 3

    @pytest.mark.asyncio
    async def test_stale_capture_cleared_before_infer(self):
        """A backend that never sets capture (or a mocked infer) must yield
        None — not the previous command's prompt from this task."""
        from inference.local_inference import set_inference_capture

        async def _infer(cmd, few_shot_examples=None, counterexamples=None):
            return "CLICK ok"   # sets nothing

        set_inference_capture("stale-prompt-from-previous-command", 99, 99)
        runner = _make_runner(_infer)
        await runner.run_local(_Cmd("click ok"))
        kwargs = runner._agent_db_mock.insert_inference.call_args.kwargs
        assert kwargs["prompt"] is None
        assert kwargs["tokens_in"] is None

    @pytest.mark.asyncio
    async def test_concurrent_run_local_no_misattribution(self):
        """Regression for the audit race: two overlapping run_local calls in
        separate tasks must each log their own prompt. With the old instance
        attributes, the slower inference's read picked up the faster task's
        prompt."""
        from inference.local_inference import set_inference_capture

        async def _infer(cmd, few_shot_examples=None, counterexamples=None):
            set_inference_capture(f"prompt::{cmd.text}")
            # Overlap window: the other task sets its capture while we sleep.
            await asyncio.sleep(0.03 if cmd.text == "slow" else 0.0)
            return f"CLICK {cmd.text}"

        runner = _make_runner(_infer)
        await asyncio.gather(
            asyncio.create_task(runner.run_local(_Cmd("slow"))),
            asyncio.create_task(runner.run_local(_Cmd("fast"))),
        )
        by_response = {
            c.kwargs["response"]: c.kwargs["prompt"]
            for c in runner._agent_db_mock.insert_inference.call_args_list
        }
        assert by_response["CLICK slow"] == "prompt::slow"
        assert by_response["CLICK fast"] == "prompt::fast"


class TestBackendsSetCapture:
    """Every HTTP backend must set the capture inside infer()."""

    @pytest.mark.asyncio
    async def test_ollama_tools_path_sets_capture(self, monkeypatch):
        from inference.local_inference import OllamaInference, get_inference_capture

        oi = OllamaInference(use_tools=True)

        async def _fake_chat(messages, tools=None):
            return {
                "message": {"content": "CLICK ok"},
                "prompt_eval_count": 42,
                "eval_count": 7,
            }

        monkeypatch.setattr(oi, "_chat", _fake_chat)
        action = await oi.infer(_Cmd("click ok"))
        assert action == "CLICK ok"
        prompt, tokens_in, tokens_out = get_inference_capture()
        assert prompt is not None and "click ok" in prompt
        assert tokens_in == 42
        assert tokens_out == 7

    def test_no_backend_keeps_instance_attributes(self):
        """The racy instance attributes must be gone everywhere."""
        import inspect
        import inference.local_inference as li
        import core.hybrid_coordinator as hcm
        for mod in (li, hcm):
            src = inspect.getsource(mod)
            assert "self.last_prompt" not in src
            assert "self.last_tokens_in" not in src
            assert "self.last_tokens_out" not in src
