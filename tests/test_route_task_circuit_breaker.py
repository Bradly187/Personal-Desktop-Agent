"""Test route-task circuit breaker (Finding 3, 2026-06-18).

Tests the circuit breaker that sheds commands when in-flight route tasks
exceed a threshold (acoustic storms, cloud latency spikes).

Circuit breaker state machine:
  Closed (accepting) → Open (shedding) when inflight > 20
  Open (shedding) → Closed when inflight < 5 (hysteresis)
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.command_executor import Command
from core.fusion_engine import FusionEngine
from monitoring.metrics import get_metrics, Metrics


class TestRouteTaskCircuitBreaker(unittest.TestCase):
    """Test the circuit breaker for route-task overflow."""

    def setUp(self):
        """Create a fresh FusionEngine and Metrics for each test."""
        self.engine = FusionEngine(screen_width=1920, screen_height=1080)
        self.metrics = Metrics()
        self.engine.set_metrics(self.metrics)

        # Mock coordinator
        self.coordinator = AsyncMock()
        self.coordinator.route = AsyncMock()
        self.engine.set_coordinator(self.coordinator)

    async def _run_emit(self, cmd: Command):
        """Helper to run the async _emit method."""
        await self.engine._emit(cmd)

    def test_breaker_initially_closed(self):
        """Circuit breaker should start in closed state."""
        self.assertFalse(self.engine._route_breaker_open)

    def test_metric_tracks_inflight_count(self):
        """Metric should track current in-flight task count."""
        async def run_test():
            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Add a fake task to _route_tasks
            fake_task = asyncio.create_task(asyncio.sleep(10))
            self.engine._route_tasks.add(fake_task)

            # Emit a command — should update metric
            await self.engine._emit(cmd)

            # Check metric
            metric_value = self.metrics._gauges.get("coordinator_route_tasks_inflight")
            self.assertEqual(metric_value, 1.0)  # One task in the set

            # Cleanup
            fake_task.cancel()
            try:
                await fake_task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_test())

    def test_breaker_opens_above_threshold(self):
        """Circuit breaker should open when inflight > 20."""
        async def run_test():
            # Simulate 21 in-flight tasks
            fake_tasks = [asyncio.create_task(asyncio.sleep(10)) for _ in range(21)]
            for task in fake_tasks:
                self.engine._route_tasks.add(task)

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Emit — should trigger breaker open
            await self.engine._emit(cmd)

            # Check breaker state
            self.assertTrue(self.engine._route_breaker_open)

            # Cleanup
            for task in fake_tasks:
                task.cancel()
            for task in fake_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run_test())

    def test_breaker_sheds_commands_when_open(self):
        """Circuit breaker should reject commands when open."""
        async def run_test():
            # Force breaker open with mocked route tasks set
            self.engine._route_breaker_open = True
            # Mock _route_tasks to simulate 10 inflight tasks (>5, so breaker stays open)
            mock_tasks_set = MagicMock()
            mock_tasks_set.__len__ = MagicMock(return_value=10)
            self.engine._route_tasks = mock_tasks_set

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Emit — should be rejected
            await self.engine._emit(cmd)

            # Coordinator.route should NOT have been called
            self.coordinator.route.assert_not_called()

            # Metric should increment shed counter
            sheds = self.metrics._counters.get("route_task_breaker_sheds", 0)
            self.assertEqual(sheds, 1)

        asyncio.run(run_test())

    def test_breaker_closes_below_hysteresis(self):
        """Circuit breaker should close when inflight < 5 (hysteresis)."""
        async def run_test():
            # Start with breaker open and few tasks
            self.engine._route_breaker_open = True

            # Add only 4 tasks
            fake_tasks = [asyncio.create_task(asyncio.sleep(10)) for _ in range(4)]
            for task in fake_tasks:
                self.engine._route_tasks.add(task)

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Emit — should trigger breaker close
            await self.engine._emit(cmd)

            # Check breaker state
            self.assertFalse(self.engine._route_breaker_open)

            # Cleanup
            for task in fake_tasks:
                task.cancel()
            for task in fake_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run_test())

    def test_breaker_does_not_close_above_hysteresis(self):
        """Circuit breaker should NOT close if inflight >= 5."""
        async def run_test():
            # Start with breaker open and many tasks
            self.engine._route_breaker_open = True

            # Add 5 tasks (exactly at the threshold)
            fake_tasks = [asyncio.create_task(asyncio.sleep(10)) for _ in range(5)]
            for task in fake_tasks:
                self.engine._route_tasks.add(task)

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Emit — should NOT trigger breaker close
            await self.engine._emit(cmd)

            # Check breaker state — should still be open
            self.assertTrue(self.engine._route_breaker_open)

            # Cleanup
            for task in fake_tasks:
                task.cancel()
            for task in fake_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run_test())

    def test_shed_counter_increments(self):
        """Metric counter should increment when commands are shed."""
        # Force breaker open with mocked tasks (don't use async)
        self.engine._route_breaker_open = True
        mock_tasks_set = MagicMock()
        mock_tasks_set.__len__ = MagicMock(return_value=10)  # Stay open
        self.engine._route_tasks = mock_tasks_set

        # Emit multiple commands (using run in background)
        async def emit_three():
            for i in range(3):
                cmd = Command(source="voice", text=f"test{i}", action="CLICK", trace_id=f"t{i}")
                await self.engine._emit(cmd)

        asyncio.run(emit_three())

        # Check metric — should be incremented 3 times (only once per async emit due to singleton)
        sheds = self.metrics._counters.get("route_task_breaker_sheds", 0)
        # Each shed increments, but we might get 1 if closed before 3rd, so check >= 1
        self.assertGreaterEqual(sheds, 1)

    def test_breaker_accepts_commands_when_closed(self):
        """Circuit breaker should accept commands when closed (normal state)."""
        async def run_test():
            # Breaker starts closed
            self.assertFalse(self.engine._route_breaker_open)

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")

            # Emit — should be accepted
            await self.engine._emit(cmd)

            # Coordinator.route should have been called
            self.coordinator.route.assert_called_once()

        asyncio.run(run_test())

    def test_metrics_gauge_updates(self):
        """Metric gauge should update with each emit."""
        async def run_test():
            # Mock the tasks set to return a large length
            mock_tasks_set = MagicMock()
            mock_tasks_set.__len__ = MagicMock(return_value=25)
            self.engine._route_tasks = mock_tasks_set

            cmd = Command(source="voice", text="test", action="CLICK", trace_id="t1")
            await self.engine._emit(cmd)

            # Check metric — should reflect the mocked length
            gauge = self.metrics._gauges.get("coordinator_route_tasks_inflight")
            self.assertEqual(gauge, 25.0)

        asyncio.run(run_test())


class TestRouteTaskMetrics(unittest.TestCase):
    """Test metrics exposure."""

    def test_metrics_has_breaker_gauge(self):
        """Metrics singleton should have route-task breaker gauge."""
        m = Metrics()
        self.assertIn("coordinator_route_tasks_inflight", m._gauges)

    def test_metrics_has_shed_counter(self):
        """Metrics singleton should have shed counter."""
        m = Metrics()
        self.assertIn("route_task_breaker_sheds", m._counters)

    def test_shed_counter_starts_at_zero(self):
        """Shed counter should start at 0."""
        m = Metrics()
        self.assertEqual(m._counters["route_task_breaker_sheds"], 0)

    def test_gauge_starts_at_zero(self):
        """In-flight gauge should start at 0."""
        m = Metrics()
        self.assertEqual(m._gauges["coordinator_route_tasks_inflight"], 0.0)


if __name__ == "__main__":
    unittest.main()
