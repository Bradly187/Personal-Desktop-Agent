#!/usr/bin/env python3
"""Benchmark a running vLLM OpenAI-compatible server.

Measures wall latency for the command-domain workload shape (short prompt,
short completion) plus a longer generation, and reports p50/p95.

Usage (WSL or Windows, stdlib only):
    python scripts/bench_vllm_server.py [--url http://localhost:8000] [--n 20]
"""

import argparse
import json
import statistics
import time
import urllib.request

PROMPT = (
    "You are a desktop command parser. Reply with exactly one verb and "
    "argument.\nUser: open the browser\nAssistant:"
)
LONG_PROMPT = "Explain the difference between a mutex and a semaphore."

# Mirrors monitoring/benchmark_models.py TEST_PROMPTS / SYSTEM_PROMPT (kept
# inline so this script stays stdlib-only and runnable from the WSL venv).
TEST_PROMPTS = [
    ("SCROLL", "scroll down three times"),
    ("SCROLL", "go up a bit"),
    ("CLICK", "click the save button"),
    ("CLICK", "select the OK option"),
    ("TYPE", "type hello world"),
    ("TYPE", "enter my name Brad"),
    ("OPEN", "open Chrome browser"),
    ("CLOSE", "close this window"),
    ("HOTKEY", "press control C to copy"),
    ("HOTKEY", "undo that with control Z"),
    ("DICTATE", "dictate the quick brown fox"),
    ("SCREENSHOT", "take a screenshot"),
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


def _post(url: str, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def bench(url: str, model: str, max_tokens: int, prompt: str, n: int, warmup: int = 3):
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    endpoint = f"{url}/v1/completions"
    for _ in range(warmup):
        _post(endpoint, payload)
    lat, tokens = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        out = _post(endpoint, payload)
        lat.append((time.perf_counter() - t0) * 1000.0)
        tokens += out["usage"]["completion_tokens"]
    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
    tput = tokens / (sum(lat) / 1000.0)
    return p50, p95, tput


def accuracy(url: str, model: str) -> None:
    """Verb-accuracy check on the 12-prompt command-classification suite."""
    correct = 0
    for expected, text in TEST_PROMPTS:
        out = _post(
            f"{url}/v1/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 32,
                "temperature": 0.0,
            },
        )
        reply = out["choices"][0]["message"]["content"].strip()
        got = reply.split()[0].upper().strip(":,.") if reply else ""
        hit = got == expected
        correct += hit
        mark = "+" if hit else "x"
        print(f"  {mark} [{expected:<10}] {text:<36} => {reply[:40]}")
    pct = round(correct / len(TEST_PROMPTS) * 100, 1)
    print(f"\n  Accuracy: {correct}/{len(TEST_PROMPTS)} ({pct}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument(
        "--accuracy", action="store_true",
        help="run the 12-prompt verb-accuracy suite instead of latency bench",
    )
    args = ap.parse_args()

    models = json.loads(
        urllib.request.urlopen(f"{args.url}/v1/models", timeout=10).read()
    )
    model_id = models["data"][0]["id"]
    print(f"server: {args.url}  model: {model_id}  n={args.n}")

    if args.accuracy:
        accuracy(args.url, model_id)
        return

    for label, prompt, max_tokens in [
        ("ttft-ish (1 tok)", PROMPT, 1),
        ("command (32 tok)", PROMPT, 32),
        ("explain (256 tok)", LONG_PROMPT, 256),
    ]:
        p50, p95, tput = bench(args.url, model_id, max_tokens, prompt, args.n)
        print(
            f"{label:<18} p50 {p50:7.1f} ms   p95 {p95:7.1f} ms   "
            f"{tput:6.1f} tok/s"
        )


if __name__ == "__main__":
    main()
