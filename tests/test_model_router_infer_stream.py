"""ModelRouter.infer_stream yields chunks when streamable, one chunk otherwise.

Real token streaming is used only for free-form, non-think-stripped local-Ollama
profiles; everything else (non-free-form command lines, vLLM pool, or a hidden
think block) falls back to a single full infer() so callers always get the same
final text.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.model_router import ModelRouter, RouterResult


def _router() -> ModelRouter:
    # Bypass the heavy __init__ (VRAM probe / profile load) — wire only what
    # infer_stream touches.
    r = ModelRouter.__new__(ModelRouter)
    r._vllm_pool = None
    return r


async def test_streamable_profile_yields_chunks():
    r = _router()
    profile = SimpleNamespace(free_form=True, thinking=False, strip_thinking=False,
                              name="gemma3:27b", system_prompt="sys")
    r.select_profile = lambda d: profile
    r._call_ollama_stream = lambda p, prompt, shot: iter(["Hel", "lo ", "world"])

    out = [tok async for tok in r.infer_stream("general", "hi")]
    assert out == ["Hel", "lo ", "world"]


async def test_non_streamable_profile_falls_back_to_single_chunk():
    r = _router()
    # Non-free-form (verb-first command line) is not streamed.
    profile = SimpleNamespace(free_form=False, thinking=False, strip_thinking=False,
                              name="llama3.1:8b")
    r.select_profile = lambda d: profile
    r.infer = AsyncMock(return_value=RouterResult(
        text="CLICK save button", model="llama3.1:8b", domain="command",
        latency_ms=1.0, free_form=False))

    out = [tok async for tok in r.infer_stream("command", "click save")]
    assert out == ["CLICK save button"]
    r.infer.assert_awaited_once()


async def test_thinking_stripped_profile_falls_back():
    r = _router()
    # free_form but thinking+strip_thinking → would leak <think> if streamed, so
    # it must take the single-shot (stripped) path.
    profile = SimpleNamespace(free_form=True, thinking=True, strip_thinking=True,
                              name="qwen3-coder:30b")
    r.select_profile = lambda d: profile
    r.infer = AsyncMock(return_value=RouterResult(
        text="def f(): pass", model="qwen3-coder:30b", domain="code",
        latency_ms=1.0, free_form=True))

    out = [tok async for tok in r.infer_stream("code", "write f")]
    assert out == ["def f(): pass"]
    r.infer.assert_awaited_once()
