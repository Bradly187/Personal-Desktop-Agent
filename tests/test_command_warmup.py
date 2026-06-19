"""Tests for command-model warm-up (OllamaInference.warmup()).

Warm-up pre-loads the command model into VRAM at startup so the FIRST real
command doesn't pay the cold-load penalty (~7.5 s observed for llama3.1:8b on a
cold RTX 5090). It posts an empty-prompt /api/generate (Ollama's model-load
request), is fire-and-forget, and must NEVER raise — a failed warm-up just falls
back to loading the model on the first command, exactly as before.

Run:
    python -m pytest tests/test_command_warmup.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.local_inference import OllamaInference, VLLMServerInference


# ---------------------------------------------------------------------------
# Mock aiohttp sessions
# ---------------------------------------------------------------------------

class _LoadResp:
    """Mock /api/generate load response (empty prompt → no tokens)."""
    status = 200
    captured: dict = {}

    async def json(self):
        return {"model": "llama3.1:8b", "done": True, "done_reason": "load", "response": ""}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _LoadSession:
    """Captures the POST json so the test can assert the empty-prompt payload."""
    def __init__(self):
        _LoadResp.captured = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def post(self, *_a, **kw):
        _LoadResp.captured = kw.get("json", {})
        return _LoadResp()


class _ErrResp:
    status = 500

    async def json(self):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _ErrSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def post(self, *_a, **_kw):
        return _ErrResp()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_warmup_success_loads_model():
    """A 200 from /api/generate marks the backend available and returns True."""
    inf = OllamaInference()
    with patch("aiohttp.ClientSession", return_value=_LoadSession()):
        ok = await inf.warmup()
    assert ok is True
    assert inf._available is True


async def test_warmup_posts_empty_prompt():
    """Warm-up must send an empty prompt — Ollama treats it as a pure model load."""
    inf = OllamaInference(model="llama3.1:8b")
    with patch("aiohttp.ClientSession", return_value=_LoadSession()):
        await inf.warmup()
    assert _LoadResp.captured.get("model") == "llama3.1:8b"
    assert _LoadResp.captured.get("prompt") == ""


# ---------------------------------------------------------------------------
# Failure paths — must never raise, must return False
# ---------------------------------------------------------------------------

async def test_warmup_http_error_returns_false():
    """A non-200 response returns False without raising."""
    inf = OllamaInference()
    with patch("aiohttp.ClientSession", return_value=_ErrSession()):
        ok = await inf.warmup()
    assert ok is False


async def test_warmup_swallows_transport_exception():
    """A transport error inside warm-up is swallowed (returns False), never raised."""
    inf = OllamaInference()
    with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
        ok = await inf.warmup()
    assert ok is False


async def test_warmup_timeout_returns_false_not_raise():
    """If the semaphore is held, the hang-timeout fires and warm-up returns False."""
    inf = OllamaInference()
    await inf._request_sem.acquire()  # simulate an in-flight call holding the lock
    try:
        with patch.dict(os.environ, {"DA_OLLAMA_TIMEOUT_S": "0.05"}):
            ok = await inf.warmup()
        assert ok is False
    finally:
        inf._request_sem.release()


async def test_warmup_does_not_trip_breaker():
    """A failed warm-up must NOT open the circuit breaker — real traffic should
    still get a fair first attempt rather than an instant fast-fail."""
    inf = OllamaInference()
    with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
        await inf.warmup()
        await inf.warmup()
        await inf.warmup()
    # fail_threshold is 3; if warm-up recorded failures the breaker would be open.
    assert inf._breaker.allow() is True


async def test_warmup_cancellation_propagates():
    """CancelledError must propagate (shutdown can cancel the fire-and-forget task)."""
    inf = OllamaInference()

    class _CancelSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def post(self, *_a, **_kw):
            raise asyncio.CancelledError()

    with patch("aiohttp.ClientSession", return_value=_CancelSession()):
        with pytest.raises(asyncio.CancelledError):
            await inf.warmup()


# ---------------------------------------------------------------------------
# ABC default — non-Ollama backends are a safe no-op
# ---------------------------------------------------------------------------

async def test_base_warmup_is_noop_for_other_backends():
    """LocalInference.warmup() defaults to a no-op so main.py can call it on any
    backend; VLLMServerInference inherits it and makes no network call."""
    server = VLLMServerInference()
    ok = await server.warmup()
    assert ok is False
