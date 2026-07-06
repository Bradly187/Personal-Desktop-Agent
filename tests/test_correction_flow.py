"""Test the correction flow: HybridCoordinator.correct() → ContinuousTrainer.

This test verifies:
1. HybridCoordinator.correct() calls ContinuousTrainer.record_correction()
2. HybridCoordinator.correct() returns correct status dict
3. Correction works when trainer is None (no-op path)
4. ContinuousTrainer.record_correction() stores the correction via AgentDB

Run: python tests/test_correction_flow.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command


@pytest.mark.asyncio
async def test_coordinator_correct_calls_trainer():
    """Verify HybridCoordinator.correct() calls the trainer."""
    from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig

    mock_trainer = MagicMock()
    mock_trainer.record_correction = AsyncMock()
    mock_trainer.memory.get_few_shot_examples = AsyncMock(return_value=[])

    coordinator = HybridCoordinator(config=CoordinatorConfig(), trainer=mock_trainer)

    cmd = Command(
        text="scroll done",
        action="",
        source="voice",
        whisper_logprob=-0.5,
    )

    result = await coordinator.correct(cmd, "TYPE done", "SCROLL down")

    assert result["status"] == "ok"
    assert result["correction"]["text"] == "scroll done"
    assert result["correction"]["wrong"] == "TYPE done"
    assert result["correction"]["correct"] == "SCROLL down"
    mock_trainer.record_correction.assert_called_once_with(cmd, "TYPE done", "SCROLL down")
    print("✓ test_coordinator_correct_calls_trainer passed")


@pytest.mark.asyncio
async def test_coordinator_correct_no_trainer():
    """Verify HybridCoordinator.correct() returns ok even with no trainer."""
    from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig

    coordinator = HybridCoordinator(config=CoordinatorConfig())

    cmd = Command(text="hot key", action="", source="voice", whisper_logprob=-0.4)

    result = await coordinator.correct(cmd, "HOTKEY", "TYPE")

    assert result["status"] == "ok"
    assert result["correction"]["wrong"] == "HOTKEY"
    assert result["correction"]["correct"] == "TYPE"
    print("✓ test_coordinator_correct_no_trainer passed")


@pytest.mark.asyncio
async def test_trainer_record_correction_stores_locally():
    """Verify ContinuousTrainer.record_correction() calls AgentDB."""
    from adaptive.continuous_trainer import ContinuousTrainer

    mock_db = AsyncMock()
    mock_db.available = True
    mock_db.memory.upsert_few_shot_example = AsyncMock()
    mock_db.get_recent_commands = AsyncMock(return_value=[])

    trainer = ContinuousTrainer(agent_db=mock_db)

    cmd = Command(
        text="oh pen notepad",
        action="",
        source="voice",
        whisper_logprob=-0.6,
    )

    await trainer.record_correction(cmd, "TYPE pen notepad", "OPEN notepad")

    mock_db.memory.upsert_few_shot_example.assert_called_once()
    call_args = mock_db.memory.upsert_few_shot_example.call_args
    assert "oh pen notepad" in str(call_args), \
        f"Expected 'oh pen notepad' in call args: {call_args}"
    print("✓ test_trainer_record_correction_stores_locally passed")


async def main():
    print("\n=== Testing Correction Flow ===\n")
    await test_coordinator_correct_calls_trainer()
    await test_coordinator_correct_no_trainer()
    await test_trainer_record_correction_stores_locally()
    print("\n=== All tests passed ✓ ===\n")


if __name__ == "__main__":
    asyncio.run(main())
