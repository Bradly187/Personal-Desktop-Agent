"""Quick benchmark of the Gemma 4 E4B-IT command classifier.

Run from WSL with the venv active:
    cd /mnt/e/Personal_Desktop_Agent
    WSL_ACTION_PROXY=http://127.0.0.1:8768 python tests/test_e4b_benchmark.py

Requires the model to be downloaded first:
    hf download cyankiwi/gemma-4-E4B-it-AWQ-INT4

Tests the full 16-verb taxonomy against the 12-prompt suite used to benchmark
Ollama's llama3.1:8b (100% baseline).  Also measures first-token latency.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ── Path setup (works whether run from project root or tests/) ────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mcp_server"))

# Ensure proxy mode so pyautogui/win32 tools are never imported in WSL
os.environ.setdefault("WSL_ACTION_PROXY", "http://127.0.0.1:8768")

from core.command_executor import Command          # noqa: E402 — after sys.path
from inference.local_inference import VLLMInference  # noqa: E402


# ---------------------------------------------------------------------------
# Test suite — 12 prompts, 9 verbs (matches original Ollama benchmark)
# ---------------------------------------------------------------------------
_TESTS: list[tuple[str, str]] = [
    # Accessibility verbs
    ("click the save button",                   "CLICK"),
    ("click on file menu",                      "CLICK"),
    ("scroll down three times",                 "SCROLL"),
    ("scroll to the top",                       "SCROLL"),
    ("type hello world",                        "TYPE"),
    ("open chrome",                             "OPEN"),
    ("close this window",                       "CLOSE"),
    ("press ctrl c",                            "HOTKEY"),
    ("take a screenshot",                       "SCREENSHOT"),
    # Ambiguous / clarify
    ("do the thing",                            "CLARIFY"),
    # Dev-agent verbs route through DomainClassifier → DevAgent, never reach
    # VLLMInference.  Testing them here would always fail — excluded intentionally.
]


async def run_benchmark(model: str, quantization: str = "awq") -> None:
    print(f"\nModel:        {model}")
    print(f"Quantization: {quantization or 'auto (from model config)'}")
    print(f"Tests:        {len(_TESTS)}\n")

    v = VLLMInference(model=model, quantization=quantization)

    # First inference warms the model — measure separately
    warm_cmd = Command(text="click ok", action="", source="bench")
    t_load = time.monotonic()
    warm_result = await v.infer(warm_cmd)
    load_ms = (time.monotonic() - t_load) * 1000
    print(f"  First inference (includes model load): {load_ms / 1000:.1f}s")
    print(f"  Result: {warm_result!r}\n")

    passed = 0
    latencies: list[float] = []

    print(f"  {'':3} {'Input':40} {'Expected':12} {'Got':30} {'ms':>6}")
    print(f"  {'-'*3} {'-'*40} {'-'*12} {'-'*30} {'-'*6}")

    for text, expected_verb in _TESTS:
        cmd = Command(text=text, action="", source="bench")
        t0 = time.monotonic()
        result = await v.infer(cmd)
        ms = (time.monotonic() - t0) * 1000
        latencies.append(ms)

        ok = result.upper().startswith(expected_verb)
        passed += ok
        mark = "✓" if ok else "✗"
        print(f"  {mark}   {text!r:40} {expected_verb:12} {result!r:30} {ms:6.0f}")

    total = len(_TESTS)
    p50 = sorted(latencies)[len(latencies) // 2]
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]

    print(f"\n  Score:   {passed}/{total}  ({100*passed//total}%)")
    print(f"  Latency: p50={p50:.0f}ms  p95={p95:.0f}ms  (warm)")
    print()

    if passed == total:
        print("  ✓ 100% on accessibility verbs — ready to proceed with 31B specialist download")
    else:
        failed = [(t, e) for (t, e), l in zip(_TESTS, latencies) if not (await _check(v, t, e))]
        print("  ✗ Accuracy below 100% — review failed cases above")
        print("    Consider: grammar constraints may need tuning, or use llama3.1:8b for command domain")


async def _check(v: VLLMInference, text: str, verb: str) -> bool:
    """Re-test a single case."""
    cmd = Command(text=text, action="", source="bench")
    r = await v.infer(cmd)
    return r.upper().startswith(verb)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="cyankiwi/gemma-4-E4B-it-AWQ-INT4")
    ap.add_argument("--quantization", default=None,
                    help="vLLM quantization mode; None = auto-detect from model config (default)")
    args = ap.parse_args()

    asyncio.run(run_benchmark(args.model, args.quantization))
