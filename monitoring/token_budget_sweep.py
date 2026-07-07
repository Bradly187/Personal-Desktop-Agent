"""Token-budget sweep for reasoning models that returned empty at num_predict=32.

For each model, run the 12-prompt verb suite at increasing num_predict budgets and
report accuracy + empty-output count per budget. Single run per prompt (trend, not
latency percentiles). Keeps each model resident across its budget sweep (no eviction).
"""
import argparse
import json
import urllib.request
import re
import time

_ap = argparse.ArgumentParser(description="Accuracy vs num_predict sweep (verb suite).")
_ap.add_argument("--models", default="", help="comma-separated models (default: gemma4 reasoning variants)")
_ap.add_argument("--budgets", default="", help="comma-separated num_predict values (default: 32,64,128,256,512)")
_ARGS, _ = _ap.parse_known_args()

MODELS = [m.strip() for m in _ARGS.models.split(",") if m.strip()] or [
    "gemma4:12b",
    "gemma4:12b-it-qat",
    "gemma4:26b-a4b-it-qat",
    "gemma4:31b-it-qat",
]
BUDGETS = [int(b) for b in _ARGS.budgets.split(",") if b.strip()] or [32, 64, 128, 256, 512]

PROMPTS = [
    ("SCROLL", "scroll down three times"), ("SCROLL", "go up a bit"),
    ("CLICK", "click the save button"), ("CLICK", "select the OK option"),
    ("TYPE", "type hello world"), ("TYPE", "enter my name Brad"),
    ("OPEN", "open Chrome browser"), ("CLOSE", "close this window"),
    ("HOTKEY", "press control C to copy"), ("HOTKEY", "undo that with control Z"),
    ("DICTATE", "dictate the quick brown fox"), ("SCREENSHOT", "take a screenshot"),
]
SYS = """You are a desktop control assistant. Convert the user's natural-language request into exactly ONE action from the following vocabulary:
CLICK <target>
SCROLL <direction> [<amount>]
TYPE <text>
OPEN <app-or-file>
CLOSE [<target>]
HOTKEY <key1> [<key2>...]
DICTATE <text>
CLARIFY <question>
SCREENSHOT
Reply with ONLY the action string, nothing else."""


def gen(model, prompt, n):
    body = json.dumps({
        "model": model,
        "prompt": f"{SYS}\n\nUser: {prompt}\nAssistant:",
        "stream": False,
        "options": {"temperature": 0, "num_predict": n},
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    raw = json.loads(urllib.request.urlopen(req, timeout=120).read()).get("response", "").strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return lines[0] if lines else ""


def unload(model):
    try:
        body = json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode()
        req = urllib.request.Request("http://localhost:11434/api/generate", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


matrix = {}
for model in MODELS:
    print(f"\n{'='*64}\nMODEL: {model}\n{'='*64}", flush=True)
    matrix[model] = {}
    # warm
    try:
        gen(model, PROMPTS[0][1], 64)
    except Exception as e:
        print(f"  warmup FAILED: {e}", flush=True)
    for n in BUDGETS:
        ok = 0
        empties = 0
        t0 = time.monotonic()
        for exp, p in PROMPTS:
            try:
                got = gen(model, p, n)
            except Exception as e:
                got = f"<ERR {e}>"
            if not got:
                empties += 1
            v = got.split()[0].upper() if got else "?"
            if v == exp:
                ok += 1
        dt = time.monotonic() - t0
        pct = round(ok / len(PROMPTS) * 100, 1)
        matrix[model][n] = (ok, empties, pct)
        print(f"  num_predict={n:>4}: {ok:>2}/12 ({pct:>5}%)  empties={empties:>2}  [{dt:.0f}s]", flush=True)
    unload(model)

# Summary matrix
print(f"\n\n{'='*64}\nACCURACY vs TOKEN BUDGET (single run/prompt)\n{'='*64}", flush=True)
hdr = "  " + f"{'model':<24}" + "".join(f"{n:>8}" for n in BUDGETS)
print(hdr, flush=True)
print("  " + "-" * (24 + 8 * len(BUDGETS)), flush=True)
for model in MODELS:
    row = "  " + f"{model:<24}"
    for n in BUDGETS:
        ok, empties, pct = matrix[model][n]
        row += f"{str(pct)+'%':>8}"
    print(row, flush=True)
print("\nDONE", flush=True)
