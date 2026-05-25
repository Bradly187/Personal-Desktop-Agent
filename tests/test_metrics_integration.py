import asyncio
import types
from unittest import mock

import pytest

from continuous_trainer import ContinuousTrainer


class DummyDB:
    available = True

    async def get_recent_routing_stats(self, limit=1000):
        return []

    async def promote_hotwords(self, thresh):
        return None

    async def get_recent_gesture_samples(self, gesture, limit=500):
        return []

    async def get_gesture_floor(self, gesture):
        return 0.5

    async def update_gesture_calibration(self, gesture, floor, count, p10):
        return None

    async def get_recent_gesture_velocities(self, gesture, limit=500):
        return []

    async def get_gesture_velocity_floor(self, gesture):
        return 0.1

    async def update_gesture_velocity_calibration(self, gesture, floor, count, p10):
        return None


@pytest.mark.asyncio
async def test_adaptation_pass_emits_metric(monkeypatch):
    # Replace the adaptation counter with a mock
    import metrics

    mock_counter = mock.MagicMock()
    monkeypatch.setattr(metrics, 'adaptation_pass_counter', mock_counter)

    db = DummyDB()
    trainer = ContinuousTrainer(agent_db=db, adaptation_interval_s=1.0)

    # Run a single adaptation pass
    await trainer._adapt()

    # Ensure the adaptation pass increments the prometheus counter
    assert mock_counter.inc.called, "adaptation_pass_counter.inc() should be called"
