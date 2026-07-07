"""ContinuousTrainer × BehavioralTwinState integration tests.

Feature: behavioral-twin-state
Properties covered:
  Property 13: Gate 1 tightening suppressed when pain_day_active=True
  Property 15: Gesture floor reduction is exactly 0.05 more on pain days
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adaptive.continuous_trainer import ContinuousTrainer


# ---------------------------------------------------------------------------
# Helpers — minimal stubs
# ---------------------------------------------------------------------------

@dataclass
class MockCoordinatorConfig:
    whisper_logprob_min: float = -0.5
    gesture_confidence_min: float = 0.6


def _make_routing_entries(
    n_local: int = 5,
    n_cloud: int = 20,
    n_clarify: int = 0,
) -> list[dict]:
    """Build routing stats that have high cloud rate and low failure rate."""
    entries = []
    for _ in range(n_local):
        entries.append({"route": "local", "action": "CLICK"})
    for _ in range(n_cloud):
        entries.append({"route": "cloud", "action": "CLICK"})
    for _ in range(n_clarify):
        entries.append({"route": "local", "action": "CLARIFY: unclear"})
    return entries


def _make_db(gesture_samples: Optional[dict[str, list[float]]] = None, gesture_floors: Optional[dict[str, float]] = None):
    """Return a mock AgentDB that has gesture samples."""
    db = MagicMock()
    db.available = True
    db.upsert_few_shot_example = AsyncMock()
    db.upsert_few_shot_counterexample = AsyncMock()
    db.get_recent_routing_stats = AsyncMock(return_value=[])
    db.promote_hotwords = AsyncMock()
    db.get_gesture_floor = AsyncMock(side_effect=lambda g: (gesture_floors or {}).get(g, 0.0))
    db.update_gesture_calibration = AsyncMock()

    samples = gesture_samples or {}

    async def _get_samples(gesture, limit=500):
        return samples.get(gesture, [])

    db.get_recent_gesture_samples = _get_samples
    return db


def _make_trainer(config=None, gesture_samples=None, gesture_floors=None):
    db = _make_db(gesture_samples=gesture_samples, gesture_floors=gesture_floors)
    return ContinuousTrainer(
        agent_db=db,
        config=config,
        twin_state=None,
        gesture_samples_min=10,
    )


# ---------------------------------------------------------------------------
# Property 13: Gate 1 tightening suppressed when pain_day_active=True
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 13: Gate 1 tightening suppressed on pain day
# Validates: Requirements 5.5, 6.3
def test_gate1_tightening_suppressed_on_pain_day():
    """When pain_day_active=True, Gate 1 threshold must NOT be changed.

    Conditions that would normally trigger loosening:
      - cloud_rate > 30%  (n_cloud=20, n_local=5 → 80%)
      - failure_rate < 10% (0 CLARIFY)
    """
    config = MockCoordinatorConfig(whisper_logprob_min=-0.5)
    trainer = _make_trainer(config=config)

    entries = _make_routing_entries(n_local=5, n_cloud=20, n_clarify=0)
    original_threshold = config.whisper_logprob_min

    asyncio.run(trainer._adapt_gate1_threshold(entries, pain_day_active=True))

    assert config.whisper_logprob_min == original_threshold, (
        f"Gate 1 threshold was changed during pain day: "
        f"{original_threshold} → {config.whisper_logprob_min}"
    )


@given(
    n_local=st.integers(min_value=0, max_value=5),
    n_cloud=st.integers(min_value=20, max_value=50),
    initial_threshold=st.floats(min_value=-2.0, max_value=-0.2, allow_nan=False),
)
@settings(max_examples=100)
def test_gate1_tightening_suppressed_on_pain_day_pbt(n_local, n_cloud, initial_threshold):
    """Property: for any routing distribution with high cloud rate and low failure rate,
    Gate 1 threshold is never modified when pain_day_active=True."""
    config = MockCoordinatorConfig(whisper_logprob_min=initial_threshold)
    trainer = _make_trainer(config=config)

    entries = _make_routing_entries(n_local=n_local, n_cloud=n_cloud, n_clarify=0)
    asyncio.run(trainer._adapt_gate1_threshold(entries, pain_day_active=True))

    assert config.whisper_logprob_min == initial_threshold, (
        f"Gate 1 threshold was modified despite pain_day_active=True: "
        f"{initial_threshold} → {config.whisper_logprob_min}"
    )


def test_gate1_tightening_allowed_without_pain_day():
    """D1 fix: Gate 1 LOOSENS (floor goes more negative) when cloud escalation is
    high and pain_day_active=False. Verifies the direction inversion is corrected."""
    config = MockCoordinatorConfig(whisper_logprob_min=-0.5)
    trainer = _make_trainer(config=config)

    entries = _make_routing_entries(n_local=5, n_cloud=20, n_clarify=0)
    original_threshold = config.whisper_logprob_min

    asyncio.run(trainer._adapt_gate1_threshold(entries, pain_day_active=False))

    assert config.whisper_logprob_min != original_threshold, (
        "Gate 1 threshold was NOT changed when conditions require loosening "
        "and pain_day_active=False"
    )
    # D1 fix: threshold must DECREASE (more negative = more permissive locally)
    assert config.whisper_logprob_min < original_threshold, (
        f"Expected threshold to decrease (loosen gate), got {config.whisper_logprob_min}"
    )


# ---------------------------------------------------------------------------
# Property 15: Gesture floor reduction is exactly 0.05 more on pain days
# ---------------------------------------------------------------------------

# Feature: behavioral-twin-state, Property 15: gesture floor -0.05 extra on pain day
# Validates: Requirements 5.5, 14.5
def test_gesture_floor_extra_reduction_on_pain_day():
    """Pain day floor == normal floor - 0.05 (clamped to 0.0).

    Build a deterministic sample set so p10 is known, then compare
    _update_gesture_calibration() output for pain_day_active=True vs False.
    """
    # 10 samples, sorted: [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.80]
    # p10_idx = max(0, int(10 * 0.10) - 1) = max(0, 0) = 0  → p10 = 0.60
    # normal floor = max(0.0, 0.60 - 0.05) = 0.55
    # pain day floor = max(0.0, 0.55 - 0.05) = 0.50
    samples = [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.80]
    gesture = "POINT"

    async def run():
        # --- Normal day ---
        normal_trainer = _make_trainer(
            gesture_samples={gesture: samples},
            gesture_floors={gesture: 0.0},
        )
        await normal_trainer._update_gesture_calibration(pain_day_active=False)
        call_args_normal = normal_trainer._db.update_gesture_calibration.call_args_list
        assert call_args_normal, "update_gesture_calibration was not called for normal day"
        normal_floor = call_args_normal[0][0][1]  # positional arg index 1 = floor

        # --- Pain day ---
        pain_trainer = _make_trainer(
            gesture_samples={gesture: samples},
            gesture_floors={gesture: 0.0},
        )
        await pain_trainer._update_gesture_calibration(pain_day_active=True)
        call_args_pain = pain_trainer._db.update_gesture_calibration.call_args_list
        assert call_args_pain, "update_gesture_calibration was not called for pain day"
        pain_floor = call_args_pain[0][0][1]

        expected_pain_floor = max(0.0, normal_floor - 0.05)
        assert abs(pain_floor - expected_pain_floor) < 1e-9, (
            f"Pain day floor {pain_floor:.4f} != normal floor {normal_floor:.4f} - 0.05 "
            f"= {expected_pain_floor:.4f}"
        )

    asyncio.run(run())


@given(
    raw_samples=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        min_size=10,
        max_size=100,
    )
)
@settings(max_examples=100)
def test_gesture_floor_extra_reduction_on_pain_day_pbt(raw_samples):
    """Property: pain_day floor == max(0, normal_floor - 0.05) for any sample set."""

    gesture = "PINCH"

    async def run():
        # Normal day
        normal_trainer = _make_trainer(
            gesture_samples={gesture: raw_samples},
            gesture_floors={gesture: 0.0},
        )
        await normal_trainer._update_gesture_calibration(pain_day_active=False)
        calls_normal = normal_trainer._db.update_gesture_calibration.call_args_list

        # Pain day
        pain_trainer = _make_trainer(
            gesture_samples={gesture: raw_samples},
            gesture_floors={gesture: 0.0},
        )
        await pain_trainer._update_gesture_calibration(pain_day_active=True)
        calls_pain = pain_trainer._db.update_gesture_calibration.call_args_list

        # Both should update (enough samples) or neither should (< gesture_min)
        assert len(calls_normal) == len(calls_pain), (
            f"Inconsistent calibration call count: normal={len(calls_normal)}, "
            f"pain={len(calls_pain)}"
        )

        if not calls_normal:
            return  # < gesture_min samples — no update expected

        normal_floor = calls_normal[0][0][1]
        pain_floor = calls_pain[0][0][1]
        expected_pain_floor = max(0.0, normal_floor - 0.05)

        assert abs(pain_floor - expected_pain_floor) < 1e-9, (
            f"PBT: pain floor {pain_floor:.6f} != expected {expected_pain_floor:.6f} "
            f"(normal_floor={normal_floor:.6f})"
        )

    asyncio.run(run())


def test_gesture_floor_clamped_to_zero_on_pain_day():
    """Pain day floor is clamped to 0.0 and never goes negative."""
    # Very low samples → normal floor near 0 → pain floor must not go below 0
    samples = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
    # p10_idx = 0 → p10 = 0.01 → normal_floor = max(0, 0.01 - 0.05) = 0.0
    # pain_floor = max(0, 0.0 - 0.05) = 0.0
    gesture = "FIST"

    async def run():
        trainer = _make_trainer(
            gesture_samples={gesture: samples},
            gesture_floors={gesture: 0.0},
        )
        await trainer._update_gesture_calibration(pain_day_active=True)
        calls = trainer._db.update_gesture_calibration.call_args_list
        assert calls, "update_gesture_calibration was not called"
        floor = calls[0][0][1]
        assert floor >= 0.0, f"Gesture floor went negative on pain day: {floor}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Tranche 1 A1: trainer.record_failure forwards to the twin ONLY (no few-shot)
# ---------------------------------------------------------------------------

@dataclass
class _FailCmd:
    text: str = "bad command"
    source: str = "voice"
    gesture_confidence: float = 0.0
    params: dict = field(default_factory=dict)


def test_trainer_record_failure_forwards_to_twin_only():
    db = _make_db()
    twin = MagicMock()
    twin.record_failure = AsyncMock()
    trainer = ContinuousTrainer(
        agent_db=db, config=None, twin_state=twin, gesture_samples_min=10
    )

    asyncio.run(trainer.record_failure(_FailCmd(), "CLICK", command_id=7))

    twin.record_failure.assert_called_once()
    # Must write NOTHING to the success-biased few-shot store
    db.upsert_few_shot_example.assert_not_called()


def test_trainer_record_failure_no_twin_is_safe():
    trainer = _make_trainer()  # twin_state=None
    asyncio.run(trainer.record_failure(_FailCmd(), "CLICK"))
    trainer._db.upsert_few_shot_example.assert_not_called()
