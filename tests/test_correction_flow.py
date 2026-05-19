"""Test the correction flow: ContinuousTrainer → AgentCore fallback client.

This test verifies:
1. HybridCoordinator.correct() calls ContinuousTrainer.record_correction()
2. ContinuousTrainer.record_correction() stores locally AND fires async task
3. AgentCoreFallbackClient.record_correction() sends the right payload

Run: python tests/test_correction_flow.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from command_executor import Command
from agentcore_fallback.client import AgentCoreFallbackClient, FallbackConfig


@pytest.mark.asyncio
async def test_client_resolve_payload():
    """Verify the client builds the correct payload for disambiguation."""
    cmd = Command(
        text="clothes the window",
        action="",
        source="voice",
        whisper_logprob=-0.8,
        gesture_confidence=1.0,
        session_context=["OPEN chrome", "CLICK address bar"],
    )

    config = FallbackConfig(dev_url="http://localhost:8090/invocations")
    client = AgentCoreFallbackClient(config)

    # Mock aiohttp to capture what would be sent
    captured_payload = {}

    class MockResponse:
        status = 200
        async def json(self):
            return {"response": "CLOSE"}
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockPost:
        def __init__(self, **kwargs):
            captured_payload.update(kwargs.get("json") or {})
        async def __aenter__(self):
            return MockResponse()
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def post(self, url, json=None, timeout=None):
            return MockPost(json=json)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    with patch("aiohttp.ClientSession", return_value=MockSession()):
        result = await client.resolve(cmd)

    assert result == "CLOSE", f"Expected 'CLOSE', got '{result}'"
    assert captured_payload["command_text"] == "clothes the window"
    assert captured_payload["source"] == "voice"
    assert captured_payload["whisper_logprob"] == -0.8
    assert captured_payload["session_context"] == ["OPEN chrome", "CLICK address bar"]
    assert "session_id" in captured_payload
    assert "actor_id" in captured_payload
    print("✓ test_client_resolve_payload passed")


@pytest.mark.asyncio
async def test_client_correction_payload():
    """Verify the client builds the correct payload for corrections."""
    config = FallbackConfig(dev_url="http://localhost:8090/invocations")
    client = AgentCoreFallbackClient(config)

    captured_payload = {}

    class MockResponse:
        status = 200
        async def json(self):
            return {"response": "Correction recorded."}
        async def text(self):
            return ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockPost:
        def __init__(self, **kwargs):
            captured_payload.update(kwargs.get("json") or {})
        async def __aenter__(self):
            return MockResponse()
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def post(self, url, json=None, timeout=None):
            return MockPost(json=json)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    with patch("aiohttp.ClientSession", return_value=MockSession()):
        result = await client.record_correction(
            original_text="clothes the window",
            wrong_action="CLICK clothes",
            correct_action="CLOSE",
        )

    assert "Correction" in result or "recorded" in result.lower() or result == "Correction recorded."
    assert captured_payload["original_text"] == "clothes the window"
    assert captured_payload["wrong_action"] == "CLICK clothes"
    assert captured_payload["correct_action"] == "CLOSE"
    assert captured_payload["session_id"] == "corrections"
    print("✓ test_client_correction_payload passed")


@pytest.mark.asyncio
async def test_coordinator_correct_method():
    """Verify HybridCoordinator.correct() calls the trainer."""
    from hybrid_coordinator import HybridCoordinator, CoordinatorConfig

    # Create coordinator with mocked trainer
    mock_trainer = MagicMock()
    mock_trainer.record_correction = AsyncMock()
    mock_trainer.get_few_shot_examples = AsyncMock(return_value=[])

    config = CoordinatorConfig(agentcore_enabled=False)  # disable AgentCore for this test
    coordinator = HybridCoordinator(config=config, trainer=mock_trainer)

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

    # Verify trainer was called
    mock_trainer.record_correction.assert_called_once_with(cmd, "TYPE done", "SCROLL down")
    print("✓ test_coordinator_correct_method passed")


@pytest.mark.asyncio
async def test_trainer_record_correction_fires_agentcore():
    """Verify ContinuousTrainer.record_correction() fires the AgentCore task."""
    from continuous_trainer import ContinuousTrainer

    mock_db = AsyncMock()
    mock_db.available = False
    trainer = ContinuousTrainer(agent_db=mock_db)
    # Don't start (no DB) — just test the AgentCore forwarding path

    cmd = Command(
        text="oh pen notepad",
        action="",
        source="voice",
        whisper_logprob=-0.6,
    )

    # Mock the AgentCore client
    mock_client = AsyncMock()
    mock_client.record_correction = AsyncMock(return_value="OK")

    with patch(
        "agentcore_fallback.client.AgentCoreFallbackClient",
        return_value=mock_client,
    ):
        await trainer.record_correction(cmd, "TYPE pen notepad", "OPEN notepad")
        # Give the fire-and-forget task time to run
        await asyncio.sleep(0.1)

    mock_client.record_correction.assert_called_once_with(
        original_text="oh pen notepad",
        wrong_action="TYPE pen notepad",
        correct_action="OPEN notepad",
    )
    print("✓ test_trainer_record_correction_fires_agentcore passed")


async def main():
    print("\n=== Testing Correction Flow ===\n")
    await test_client_resolve_payload()
    await test_client_correction_payload()
    await test_coordinator_correct_method()
    await test_trainer_record_correction_fires_agentcore()
    print("\n=== All tests passed ✓ ===\n")


if __name__ == "__main__":
    asyncio.run(main())
