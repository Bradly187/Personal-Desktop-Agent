"""Unit tests for IPadBridge.set_feature_toggle handler.

Tests the set_feature_toggle message handling in ipad_bridge.py:
- Valid feature names produce ack with status "ok"
- Unknown feature names produce ack with status "error"
- Toggle state is forwarded to FusionEngine

Run:
    pytest tests/test_set_feature_toggle.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def bridge():
    """Create an IPadBridge instance with mocked dependencies."""
    with patch.dict("sys.modules", {"pyautogui": MagicMock()}):
        from core.ipad_bridge import IPadBridge
        b = IPadBridge(port=9999)
        # Mock FusionEngine
        b._fusion = MagicMock()
        b._fusion.set_feature_toggle = MagicMock()
        return b


@pytest.fixture
def mock_ws():
    """Create a mock WebSocket response."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


class TestSetFeatureToggle:
    """Tests for the set_feature_toggle message handler."""

    @pytest.mark.asyncio
    async def test_valid_feature_toggle_enabled(self, bridge, mock_ws):
        """Valid feature name with enabled=true returns ok ack."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft1",
            "feature": "edge_scroll",
            "enabled": True,
        })
        await bridge._handle_message(mock_ws, msg)

        mock_ws.send_json.assert_called_once_with({
            "type": "ack",
            "id": "ft1",
            "status": "ok",
            "feature": "edge_scroll",
            "enabled": True,
        })

    @pytest.mark.asyncio
    async def test_valid_feature_toggle_disabled(self, bridge, mock_ws):
        """Valid feature name with enabled=false returns ok ack."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft2",
            "feature": "gaze_dwell_click",
            "enabled": False,
        })
        await bridge._handle_message(mock_ws, msg)

        mock_ws.send_json.assert_called_once_with({
            "type": "ack",
            "id": "ft2",
            "status": "ok",
            "feature": "gaze_dwell_click",
            "enabled": False,
        })

    @pytest.mark.asyncio
    async def test_unknown_feature_returns_error(self, bridge, mock_ws):
        """Unknown feature name returns error ack."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft3",
            "feature": "nonexistent_feature",
            "enabled": True,
        })
        await bridge._handle_message(mock_ws, msg)

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["type"] == "ack"
        assert call_args["id"] == "ft3"
        assert call_args["status"] == "error"
        assert "nonexistent_feature" in call_args["error"]

    @pytest.mark.asyncio
    async def test_forwards_to_fusion_engine(self, bridge, mock_ws):
        """Toggle state is forwarded to FusionEngine.set_feature_toggle()."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft4",
            "feature": "gaze_cursor_mode",
            "enabled": False,
        })
        await bridge._handle_message(mock_ws, msg)

        bridge._fusion.set_feature_toggle.assert_called_once_with("gaze_cursor_mode", False)

    @pytest.mark.asyncio
    async def test_no_fusion_engine_still_acks(self, bridge, mock_ws):
        """If FusionEngine not wired, still responds with ok ack (no crash)."""
        bridge._fusion = None
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft5",
            "feature": "edge_scroll",
            "enabled": True,
        })
        await bridge._handle_message(mock_ws, msg)

        mock_ws.send_json.assert_called_once_with({
            "type": "ack",
            "id": "ft5",
            "status": "ok",
            "feature": "edge_scroll",
            "enabled": True,
        })

    @pytest.mark.asyncio
    async def test_all_valid_features_accepted(self, bridge, mock_ws):
        """All 6 valid feature names are accepted."""
        valid_features = [
            "gaze_dwell_click", "gaze_dwell_right_click",
            "gaze_dwell_double_click", "gaze_dwell_drag",
            "edge_scroll", "gaze_cursor_mode",
        ]
        for feature in valid_features:
            mock_ws.reset_mock()
            msg = json.dumps({
                "type": "set_feature_toggle",
                "id": f"ft_{feature}",
                "feature": feature,
                "enabled": True,
            })
            await bridge._handle_message(mock_ws, msg)

            call_args = mock_ws.send_json.call_args[0][0]
            assert call_args["status"] == "ok", f"Feature {feature} should be valid"
            assert call_args["feature"] == feature

    @pytest.mark.asyncio
    async def test_enabled_coerced_to_bool(self, bridge, mock_ws):
        """The enabled field is coerced to bool (truthy/falsy values)."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft6",
            "feature": "edge_scroll",
            "enabled": 1,  # truthy int
        })
        await bridge._handle_message(mock_ws, msg)

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["enabled"] is True

    @pytest.mark.asyncio
    async def test_none_feature_returns_error(self, bridge, mock_ws):
        """Missing/None feature field returns error ack."""
        msg = json.dumps({
            "type": "set_feature_toggle",
            "id": "ft7",
            "enabled": True,
        })
        await bridge._handle_message(mock_ws, msg)

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["status"] == "error"


class TestFusionEngineSetFeatureToggle:
    """Tests for FusionEngine.set_feature_toggle method."""

    def test_set_valid_feature(self):
        """Setting a valid feature updates the toggle dict."""
        from core.fusion_engine import FusionEngine
        fe = FusionEngine(1920, 1080)

        fe.set_feature_toggle("edge_scroll", False)
        assert fe._feature_toggles["edge_scroll"] is False

        fe.set_feature_toggle("edge_scroll", True)
        assert fe._feature_toggles["edge_scroll"] is True

    def test_all_features_default_true(self):
        """All feature toggles default to True."""
        from core.fusion_engine import FusionEngine
        fe = FusionEngine(1920, 1080)

        for feature, enabled in fe._feature_toggles.items():
            assert enabled is True, f"{feature} should default to True"

    def test_unknown_feature_ignored(self):
        """Setting an unknown feature does not crash or modify state."""
        from core.fusion_engine import FusionEngine
        fe = FusionEngine(1920, 1080)

        original = dict(fe._feature_toggles)
        fe.set_feature_toggle("nonexistent", True)
        assert fe._feature_toggles == original
