"""Tests for OllamaInference hang-timeout guard (FINDING 4).

DA_OLLAMA_TIMEOUT_S wraps both _request_sem acquisition and the Ollama HTTP
call inside asyncio.timeout(). This prevents a hung Ollama backend (CUDA OOM,
stalled runner, model-load freeze) from blocking subsequent inferences via the
serialising semaphore for more than the configured window (default 45 s).

Run:
    python -m pytest tests/test_ollama_timeout.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from inference.local_inference import OllamaInference
from monitoring.metrics import Metrics


def _cmd(text: str = "click save") -> Command:
    return Command(text=text, action="", source="voice", whisper_logprob=0.0)


@pytest.fixture
def inf() -> OllamaInference:
    """Fresh OllamaInference instance with metrics wired."""
    instance = OllamaInference()
    instance.set_metrics(Metrics())
    return instance


# ---------------------------------------------------------------------------
# Hang-timeout fires
# ---------------------------------------------------------------------------

async def test_timeout_fires_when_semaphore_held(inf):
    """If _request_sem is held indefinitely, the outer timeout fires and CLARIFY is returned."""
    # Acquire the semaphore externally to simulate a hung prior call
    await inf._request_sem.acquire()
    try:
        with patch.dict(os.environ, {"DA_OLLAMA_TIMEOUT_S": "0.05"}):
            result = await inf.infer(_cmd())

        assert result.startswith("CLARIFY")
        assert inf._metrics._counters["ollama_hang_detected"] == 1
        assert inf._available is False
    finally:
        inf._request_sem.release()


async def test_timeout_fires_when_http_hangs(inf):
    """If the HTTP request hangs past DA_OLLAMA_TIMEOUT_S, CLARIFY is returned."""
    class _HangingSession:
        """aiohttp.ClientSession mock whose POST never responds."""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def post(self, *_a, **_kw):
            return _HangingResponse()

    class _HangingResponse:
        async def __aenter__(self):
            await asyncio.sleep(9999)   # never finishes
            return self
        async def __aexit__(self, *_):
            pass

    with (
        patch.dict(os.environ, {"DA_OLLAMA_TIMEOUT_S": "0.05"}),
        patch("aiohttp.ClientSession", return_value=_HangingSession()),
    ):
        result = await inf.infer(_cmd())

    assert result.startswith("CLARIFY")
    assert inf._metrics._counters["ollama_hang_detected"] == 1
    assert inf._available is False


async def test_timeout_increments_metric_once_per_hang(inf):
    """Each hang increments ollama_hang_detected by exactly 1."""
    await inf._request_sem.acquire()
    try:
        with patch.dict(os.environ, {"DA_OLLAMA_TIMEOUT_S": "0.05"}):
            await inf.infer(_cmd())
            await inf.infer(_cmd())
        assert inf._metrics._counters["ollama_hang_detected"] == 2
    finally:
        inf._request_sem.release()


# ---------------------------------------------------------------------------
# Normal path unaffected
# ---------------------------------------------------------------------------

async def test_normal_inference_not_affected(inf):
    """A fast successful response completes without triggering the timeout."""
    class _OkSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def post(self, *_a, **_kw):
            return _OkResp()

    class _OkResp:
        status = 200
        async def json(self):
            return {
                "response": "CLICK save button",
                "prompt_eval_count": 10,
                "eval_count": 3,
            }
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    with (
        patch.dict(os.environ, {"DA_OLLAMA_TIMEOUT_S": "10.0"}),
        patch("aiohttp.ClientSession", return_value=_OkSession()),
    ):
        result = await inf.infer(_cmd())

    assert result == "CLICK save button"
    assert inf._metrics._counters["ollama_hang_detected"] == 0
    assert inf._available is True


# ---------------------------------------------------------------------------
# Metric catalogue
# ---------------------------------------------------------------------------

async def test_hang_counter_in_metrics_catalogue():
    """ollama_hang_detected is registered in the Metrics counters dict."""
    m = Metrics()
    assert "ollama_hang_detected" in m._counters
    assert m._counters["ollama_hang_detected"] == 0


async def test_hang_counter_increments_via_inc():
    """Metrics.inc('ollama_hang_detected') works as expected."""
    m = Metrics()
    m.inc("ollama_hang_detected")
    m.inc("ollama_hang_detected")
    assert m._counters["ollama_hang_detected"] == 2


async def test_hang_counter_visible_in_snapshot():
    """ollama_hang_detected appears in get_snapshot()['counters']."""
    m = Metrics()
    m.inc("ollama_hang_detected")
    snap = m.get_snapshot()
    assert snap["counters"]["ollama_hang_detected"] == 1


# ---------------------------------------------------------------------------
# set_metrics wiring
# ---------------------------------------------------------------------------

async def test_set_metrics_wires_correctly():
    """set_metrics() replaces the metrics reference and set_metrics(None) clears it."""
    instance = OllamaInference()
    assert instance._metrics is None

    m = Metrics()
    instance.set_metrics(m)
    assert instance._metrics is m

    instance.set_metrics(None)
    assert instance._metrics is None
