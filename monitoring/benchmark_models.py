"""Model benchmark — measures latency and accuracy for all pulled Ollama models.

Tests each model against 12 representative commands covering all 9 action verbs,
measures p50/p95 latency over 3 runs per prompt, checks VRAM before/after load,
and prints a ranked recommendation table.

Usage:
    python benchmark_models.py [--runs 3] [--models model1,model2]

Output:
    Console table + benchmark_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

# Ensure the project root is importable when run as `python monitoring/benchmark_models.py`
# from the repo root, so `from storage.db import AnalyticsDB` resolves (DuckDB persistence).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Test suite — 12 prompts spanning all 9 verbs
# ---------------------------------------------------------------------------

TEST_PROMPTS = [
    # verb          natural language input
    ("SCROLL",      "scroll down three times"),
    ("SCROLL",      "go up a bit"),
    ("CLICK",       "click the save button"),
    ("CLICK",       "select the OK option"),
    ("TYPE",        "type hello world"),
    ("TYPE",        "enter my name Brad"),
    ("OPEN",        "open Chrome browser"),
    ("CLOSE",       "close this window"),
    ("HOTKEY",      "press control C to copy"),
    ("HOTKEY",      "undo that with control Z"),
    ("DICTATE",     "dictate the quick brown fox"),
    ("SCREENSHOT",  "take a screenshot"),
]

SYSTEM_PROMPT = """\
You are a desktop control assistant. Convert the user's natural-language \
request into exactly ONE action from the following vocabulary:

CLICK <target>       — click a named UI element or coordinates
SCROLL <direction> [<amount>]  — scroll up/down/left/right
TYPE <text>          — type literal text
OPEN <app-or-file>   — open an application or file
CLOSE [<target>]     — close the active or named window
HOTKEY <key1> [<key2>...]  — press a key combination
DICTATE <text>       — paste text verbatim via clipboard
CLARIFY <question>   — ask the user to clarify; do not act
SCREENSHOT           — capture the desktop screen

Reply with ONLY the action string, nothing else.\
"""


# ---------------------------------------------------------------------------
# NVML helper
# ---------------------------------------------------------------------------

def _vram_used_gb() -> float | None:
    try:
        import pynvml as nvml
        nvml.nvmlInit()
        h = nvml.nvmlDeviceGetHandleByIndex(0)
        info = nvml.nvmlDeviceGetMemoryInfo(h)
        nvml.nvmlShutdown()
        return round(info.used / (1024 ** 3), 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _list_models() -> list[dict]:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return [m for m in data.get("models", []) if m.get("size", 0) > 0]
    except Exception as exc:
        print(f"ERROR: cannot reach Ollama — {exc}")
        sys.exit(1)


def _generate(model: str, prompt: str, timeout: float = 30.0) -> tuple[str, float, dict]:
    """Return (response_text, wall_latency_ms, timing).

    `timing` is Ollama's server-side duration breakdown (ms), which separates the
    one-off model **load** from the actual **compute**. Wall-clock latency alone
    is misleading: on Ollama 0.30 + Windows/CUDA a (re)load adds ~2.2 s, and even a
    resident model reports ~135 ms `load_ms` per request (mmap is disabled for
    windows_cuda). The number that reflects steady-state user latency is
    `compute_ms` = prompt_eval + eval, which excludes load. Raises on HTTP error.
    """
    body = json.dumps({
        "model": model,
        "prompt": f"{SYSTEM_PROMPT}\n\nUser: {prompt}\nAssistant:",
        "stream": False,
        # 256, not 32: reasoning models (deepseek-r1, gpt-oss, gemma4) spend a
        # multi-token preamble before the answer. At 32 they hit the length cap
        # mid-reasoning and return an empty `response` (done_reason="length"),
        # which the scorer reads as a wrong answer (confirmed 2026-06-07:
        # gemma4:12b 0/12 @32 → 9/12 @256).
        "options": {"temperature": 0.0, "num_predict": 256},
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    latency_ms = (time.monotonic() - t0) * 1000
    raw = data.get("response", "").strip()
    # Some Ollama reasoning models emit their chain-of-thought in a separate
    # `thinking` field and leave only the answer in `response`; if `response`
    # came back empty, fall back to stripping any inline reasoning from `thinking`.
    if not raw:
        raw = data.get("thinking", "").strip()
    # deepseek-r1 (and others) wrap reasoning inline in <think>...</think>; strip it
    import re as _re
    raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    text = lines[0] if lines else ""

    # Server-side breakdown (nanoseconds → ms). load is one-off / per-request
    # overhead; compute is what the user actually waits on once the model is warm.
    _ms = lambda k: round(data.get(k, 0) / 1e6, 1)
    eval_count = data.get("eval_count", 0) or 0
    eval_dur_ns = data.get("eval_duration", 0) or 0
    timing = {
        "load_ms": _ms("load_duration"),
        "prompt_eval_ms": _ms("prompt_eval_duration"),
        "eval_ms": _ms("eval_duration"),
        "compute_ms": round((data.get("prompt_eval_duration", 0) + eval_dur_ns) / 1e6, 1),
        "tok_per_s": round(eval_count / (eval_dur_ns / 1e9), 1) if eval_dur_ns else None,
    }
    return text, latency_ms, timing


def _unload_model(model: str) -> None:
    """Ask Ollama to evict the model from VRAM (keep_alive=0)."""
    try:
        body = json.dumps({
            "model": model, "prompt": "", "stream": False,
            "keep_alive": 0,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Benchmark one model
# ---------------------------------------------------------------------------

def _benchmark_model(model_name: str, runs: int) -> dict:
    print(f"\n  {'-' * 60}")
    print(f"  Model: {model_name}")
    print(f"  {'-' * 60}")

    vram_before = _vram_used_gb()

    # Warm-up with the FULL system prompt so the prefix KV cache is primed.
    # Using just "click OK" would prime a different context and cause every
    # subsequent request to miss the prefix cache (~2100 ms overhead each).
    print("  Warming up (loading + priming KV prefix cache) ...", end="", flush=True)
    try:
        _generate(model_name, TEST_PROMPTS[0][1], timeout=120.0)
    except Exception as exc:
        print(f" FAILED: {exc}")
        return {"model": model_name, "error": str(exc)}
    print(" done")

    vram_after_load = _vram_used_gb()
    vram_delta = (
        round(vram_after_load - vram_before, 1)
        if vram_before is not None and vram_after_load is not None
        else None
    )

    results = []
    correct = 0
    all_latencies = []
    all_compute = []   # load-excluded server-side compute (the warm-latency signal)

    for expected_verb, prompt in TEST_PROMPTS:
        latencies = []
        computes = []
        last_response = ""
        for _ in range(runs):
            try:
                response, ms, timing = _generate(model_name, prompt, timeout=30.0)
                latencies.append(ms)
                if timing.get("compute_ms"):
                    computes.append(timing["compute_ms"])
                last_response = response
            except Exception as exc:
                print(f"    ERROR on {prompt!r}: {exc}")
                latencies.append(float("inf"))

        s = sorted(latencies)
        p50 = s[len(s) // 2]
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        all_latencies.extend(latencies)
        all_compute.extend(computes)
        compute_p50 = round(sorted(computes)[len(computes) // 2], 1) if computes else None

        got_verb = last_response.split()[0].upper() if last_response else "?"
        hit = got_verb == expected_verb
        if hit:
            correct += 1
        mark = "+" if hit else "X"

        print(f"  {mark} [{expected_verb:<10}] {prompt:<36}  =>  {last_response[:30]:<30}  "
              f"p50={p50:.0f}ms  compute={compute_p50}ms")
        results.append({
            "prompt": prompt,
            "expected": expected_verb,
            "got": last_response,
            "correct": hit,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "compute_p50_ms": compute_p50,
        })

    finite = [l for l in all_latencies if l != float("inf")]
    sf = sorted(finite)
    overall_p50 = round(sf[len(sf) // 2], 1) if sf else None
    overall_p95 = round(sf[min(len(sf) - 1, int(len(sf) * 0.95))], 1) if sf else None
    sc = sorted(all_compute)
    compute_p50 = round(sc[len(sc) // 2], 1) if sc else None
    compute_p95 = round(sc[min(len(sc) - 1, int(len(sc) * 0.95))], 1) if sc else None
    accuracy = round(correct / len(TEST_PROMPTS) * 100, 1)

    print(f"\n  Accuracy: {correct}/{len(TEST_PROMPTS)} ({accuracy}%)")
    print(f"  Wall latency:  p50={overall_p50}ms  p95={overall_p95}ms  (includes model load)")
    print(f"  Compute only:  p50={compute_p50}ms  p95={compute_p95}ms  (load-excluded — warm-latency signal)")
    print(f"  VRAM:     before={vram_before} GB  after={vram_after_load} GB  delta=+{vram_delta} GB")

    _unload_model(model_name)

    return {
        "model": model_name,
        "accuracy_pct": accuracy,
        "correct": correct,
        "total": len(TEST_PROMPTS),
        "p50_ms": overall_p50,
        "p95_ms": overall_p95,
        "compute_p50_ms": compute_p50,
        "compute_p95_ms": compute_p95,
        "vram_before_gb": vram_before,
        "vram_after_load_gb": vram_after_load,
        "vram_delta_gb": vram_delta,
        "prompts": results,
    }


# ---------------------------------------------------------------------------
# Summary + recommendation
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict], baseline_gb: float | None) -> None:
    # System context
    try:
        import pynvml as nvml
        nvml.nvmlInit()
        h = nvml.nvmlDeviceGetHandleByIndex(0)
        info = nvml.nvmlDeviceGetMemoryInfo(h)
        name_b = nvml.nvmlDeviceGetName(h)
        gpu_name = name_b.decode() if isinstance(name_b, bytes) else name_b
        total_gb = round(info.total / (1024 ** 3), 1)
        nvml.nvmlShutdown()
    except Exception:
        gpu_name, total_gb = "unknown GPU", "?"

    whisper_gb = 4.2  # measured 2026-05-08
    free_after_whisper = (total_gb - (baseline_gb or 8.3) - whisper_gb) if isinstance(total_gb, float) else "?"

    print(f"\n\n  {'=' * 70}")
    print(f"  BENCHMARK SUMMARY -- {gpu_name} ({total_gb} GB total)")
    print(f"  Baseline: {baseline_gb} GB  |  +Whisper: {whisper_gb} GB  |  Free alongside Whisper: ~{free_after_whisper:.1f} GB")
    print(f"  {'=' * 70}")
    print(f"  {'Model':<28}  {'Accuracy':>8}  {'wall p50':>9}  {'compute p50':>11}  {'VRAM':>8}  {'Fits w/Whisper':>14}")
    print(f"  {'-' * 28}  {'-' * 8}  {'-' * 9}  {'-' * 11}  {'-' * 8}  {'-' * 14}")

    valid = [r for r in results if "error" not in r]
    valid.sort(key=lambda r: (-r["accuracy_pct"], r["p50_ms"] or 9999))

    for r in valid:
        delta = r["vram_delta_gb"]
        fits = "yes" if (delta is not None and isinstance(free_after_whisper, float) and delta < free_after_whisper) else "check"
        compute = r.get("compute_p50_ms")
        print(
            f"  {r['model']:<28}  {r['accuracy_pct']:>7}%  "
            f"{str(r['p50_ms'])+'ms':>9}  {(str(compute)+'ms') if compute is not None else 'n/a':>11}  "
            f"{'+'+str(delta)+' GB':>8}  {fits:>14}"
        )

    print(f"\n  Recommendation:")
    if valid:
        best = valid[0]
        fastest = min(valid, key=lambda r: r["p50_ms"] or 9999)
        print(f"    Primary  (best accuracy): {best['model']}  --  {best['accuracy_pct']}% acc, {best['p50_ms']}ms p50")
        if fastest["model"] != best["model"]:
            print(f"    Fallback (lowest latency): {fastest['model']}  --  {fastest['accuracy_pct']}% acc, {fastest['p50_ms']}ms p50")
    print(f"  {'=' * 70}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _benchmark_vllm(model: str, runs: int) -> dict:
    """Benchmark VLLMInference against the same 12-prompt suite."""
    print(f"\n  {'-' * 60}")
    print(f"  Backend: vllm  |  Model: {model}")
    print(f"  {'-' * 60}")

    from inference.local_inference import VLLMInference
    from core.command_executor import Command

    backend = VLLMInference(model=model)

    # Warm-up load
    print("  Loading engine (first call — downloads weights if absent) ...", flush=True)
    t_load = time.monotonic()
    try:
        warm_cmd = Command(text="click OK", action="", source="benchmark")
        asyncio.run(backend.infer(warm_cmd))
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return {"model": f"vllm:{model}", "error": str(exc)}
    print(f"  Engine ready in {(time.monotonic() - t_load):.1f}s")

    vram_after_load = _vram_used_gb()

    results = []
    correct = 0
    all_latencies: list[float] = []

    for expected_verb, prompt_text in TEST_PROMPTS:
        latencies: list[float] = []
        last_response = ""
        for _ in range(runs):
            cmd = Command(text=prompt_text, action="", source="benchmark")
            t0 = time.monotonic()
            try:
                response = asyncio.run(backend.infer(cmd))
                latencies.append((time.monotonic() - t0) * 1000)
                last_response = response
            except Exception as exc:
                print(f"    ERROR: {exc}")
                latencies.append(float("inf"))

        s = sorted(latencies)
        p50 = s[len(s) // 2]
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        all_latencies.extend(latencies)

        got_verb = last_response.split()[0].upper() if last_response else "?"
        hit = got_verb == expected_verb
        if hit:
            correct += 1
        mark = "+" if hit else "X"
        print(f"  {mark} [{expected_verb:<10}] {prompt_text:<36}  =>  {last_response[:30]:<30}  p50={p50:.0f}ms")
        results.append({
            "prompt": prompt_text, "expected": expected_verb,
            "got": last_response, "correct": hit,
            "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
        })

    finite = [l for l in all_latencies if l != float("inf")]
    sf = sorted(finite)
    overall_p50 = round(sf[len(sf) // 2], 1) if sf else None
    overall_p95 = round(sf[min(len(sf) - 1, int(len(sf) * 0.95))], 1) if sf else None
    accuracy = round(correct / len(TEST_PROMPTS) * 100, 1)

    print(f"\n  Accuracy: {correct}/{len(TEST_PROMPTS)} ({accuracy}%)")
    print(f"  Latency:  p50={overall_p50}ms  p95={overall_p95}ms")
    print(f"  VRAM after load: {vram_after_load} GB")

    return {
        "model": f"vllm:{model}",
        "accuracy_pct": accuracy, "correct": correct, "total": len(TEST_PROMPTS),
        "p50_ms": overall_p50, "p95_ms": overall_p95,
        "vram_before_gb": None, "vram_after_load_gb": vram_after_load, "vram_delta_gb": None,
        "prompts": results,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Benchmark Ollama and vLLM models for desktop agent command classification")
    p.add_argument("--runs", type=int, default=3, help="Inference runs per prompt for latency stats (default: 3)")
    p.add_argument("--models", type=str, default="", help="Comma-separated Ollama model names (default: all pulled)")
    p.add_argument("--vllm", type=str, default="", metavar="HF_MODEL",
                   help="Also benchmark VLLMInference with this HuggingFace model ID "
                        "(e.g. meta-llama/Meta-Llama-3.1-8B-Instruct). Requires: pip install vllm")
    args = p.parse_args()

    all_results: list[dict] = []
    baseline_gb = _vram_used_gb()
    print(f"VRAM baseline: {baseline_gb} GB used")

    # --- vLLM benchmark (optional) ---
    if args.vllm:
        print(f"\n[vLLM] Benchmarking {args.vllm} with {args.runs} run(s) × {len(TEST_PROMPTS)} prompts")
        vllm_result = _benchmark_vllm(args.vllm, args.runs)
        all_results.append(vllm_result)

    # --- Ollama benchmark ---
    available = _list_models()
    if args.models:
        names = [m.strip() for m in args.models.split(",")]
    else:
        names = [m["name"] for m in available]
        sizes = {m["name"]: round(m["size"] / (1024**3), 1) for m in available}
        print(f"\nAvailable Ollama models: {', '.join(f'{n} ({sizes[n]}GB)' for n in names)}")

    print(f"\nRunning {args.runs} run(s) × {len(TEST_PROMPTS)} prompts per Ollama model\n")
    for name in names:
        result = _benchmark_model(name, args.runs)
        all_results.append(result)

    _print_summary(all_results, baseline_gb)
    _save_to_analytics(all_results)


def _save_to_analytics(all_results: list[dict]) -> None:
    """Persist benchmark results to analytics.duckdb (preferred) with JSON fallback."""
    try:
        from storage.db import AnalyticsDB
        import subprocess as _sp
        git_hash: str | None = None
        try:
            git_hash = _sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=_sp.DEVNULL, text=True,
            ).strip()
        except Exception:
            pass

        analytics = AnalyticsDB()
        analytics.open(Path("analytics.duckdb"))
        if not analytics.available:
            raise RuntimeError("DuckDB unavailable")

        run_id = analytics.insert_benchmark_run(
            ts=time.time(),
            git_hash=git_hash,
            mode="standard",
        )
        for r in all_results:
            if "error" in r:
                result_id = analytics.insert_benchmark_result(
                    run_id=run_id,
                    model=r["model"],
                    accuracy_pct=None, correct=None, total=None,
                    p50_ms=None, p95_ms=None,
                    vram_before_gb=None, vram_after_gb=None, vram_delta_gb=None,
                    error=r["error"],
                )
                continue
            result_id = analytics.insert_benchmark_result(
                run_id=run_id,
                model=r["model"],
                accuracy_pct=r.get("accuracy_pct"),
                correct=r.get("correct"),
                total=r.get("total"),
                p50_ms=r.get("p50_ms"),
                p95_ms=r.get("p95_ms"),
                vram_before_gb=r.get("vram_before_gb"),
                vram_after_gb=r.get("vram_after_load_gb"),
                vram_delta_gb=r.get("vram_delta_gb"),
            )
            for p in r.get("prompts", []):
                analytics.insert_benchmark_prompt(
                    result_id=result_id,
                    prompt=p["prompt"],
                    expected=p["expected"],
                    got=p.get("got"),
                    correct=bool(p.get("correct")),
                    p50_ms=p.get("p50_ms"),
                    p95_ms=p.get("p95_ms"),
                )
        analytics.close()
        print(f"  Results saved to analytics.duckdb (run_id={run_id})\n")
    except Exception as exc:
        # Fallback to JSON so a missing duckdb dependency doesn't break benchmarking
        print(f"  [WARN] analytics.duckdb unavailable ({exc}), falling back to JSON")
        out = Path("benchmark_results.json")
        out.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
        print(f"  Full results saved to {out}\n")


if __name__ == "__main__":
    main()
