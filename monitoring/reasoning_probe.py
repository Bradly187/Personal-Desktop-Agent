"""Phase 1: extend token sweep for the 26B MoE (and confirm 12b/31b ceilings) at 768/1024.
Phase 2: reasoning probe — general/dev-style questions with verifiable answers, scored
by extracting a final 'ANSWER:' line. Reasoning models get a generous budget (1024).
"""
import argparse, json, urllib.request, re, time

OLLAMA = "http://localhost:11434/api/generate"

_ap = argparse.ArgumentParser(description="Reasoning probe + 26B-MoE saturation extension.")
_ap.add_argument("--models", default="", help="comma-separated models for the reasoning probe")
_ARGS, _ = _ap.parse_known_args()


def gen(model, prompt, n, system=None, timeout=180):
    full = f"{system}\n\n{prompt}" if system else prompt
    body = json.dumps({
        "model": model, "prompt": full, "stream": False,
        "options": {"temperature": 0, "num_predict": n},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return d.get("response", "").strip(), d.get("done_reason", "")


def unload(model):
    try:
        body = json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode()
        req = urllib.request.Request(OLLAMA, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


# ---------------------------------------------------------------- Phase 1
CMD_PROMPTS = [
    ("SCROLL", "scroll down three times"), ("SCROLL", "go up a bit"),
    ("CLICK", "click the save button"), ("CLICK", "select the OK option"),
    ("TYPE", "type hello world"), ("TYPE", "enter my name Brad"),
    ("OPEN", "open Chrome browser"), ("CLOSE", "close this window"),
    ("HOTKEY", "press control C to copy"), ("HOTKEY", "undo that with control Z"),
    ("DICTATE", "dictate the quick brown fox"), ("SCREENSHOT", "take a screenshot"),
]
CMD_SYS = """You are a desktop control assistant. Convert the user's natural-language request into exactly ONE action from the following vocabulary:
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


def cmd_score(model, n):
    ok = empties = 0
    for exp, p in CMD_PROMPTS:
        try:
            raw, _ = gen(model, f"User: {p}\nAssistant:", n, system=CMD_SYS, timeout=120)
        except Exception:
            raw = ""
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        got = lines[0] if lines else ""
        if not got:
            empties += 1
        if (got.split()[0].upper() if got else "?") == exp:
            ok += 1
    return ok, empties


print("=" * 64, flush=True)
print("PHASE 1 — extended command-suite sweep (saturation)", flush=True)
print("=" * 64, flush=True)
EXT = {
    "gemma4:26b-a4b-it-qat": [640, 768, 1024],
    "gemma4:12b": [1024],
    "gemma4:31b-it-qat": [1024],
}
for model, budgets in EXT.items():
    print(f"\nMODEL: {model}", flush=True)
    try:
        gen(model, "User: click OK\nAssistant:", 64, system=CMD_SYS, timeout=120)
    except Exception as e:
        print(f"  warmup FAILED: {e}", flush=True)
    for n in budgets:
        t0 = time.monotonic()
        ok, empties = cmd_score(model, n)
        print(f"  num_predict={n:>4}: {ok:>2}/12 ({round(ok/12*100,1):>5}%)  empties={empties:>2}  [{time.monotonic()-t0:.0f}s]", flush=True)
    unload(model)


# ---------------------------------------------------------------- Phase 2
PROBE_SYS = "You are a careful reasoning assistant. Solve the problem. Reason briefly if needed, then end your reply with a single final line in the form 'ANSWER: <answer>'."
PROBE = [
    ("A train travels 60 miles in 1.5 hours. What is its average speed in mph?", ["40"]),
    ("If 3x + 7 = 22, what is x?", ["5"]),
    ("All cats are mammals. Some mammals are dogs. Does it follow that all cats are dogs? Answer Yes or No.", ["no"]),
    ("I have 12 apples. I give away half, then buy 5 more. How many apples do I have now?", ["11"]),
    ("What integer does this Python expression evaluate to: len([x for x in range(10) if x % 2 == 0])", ["5"]),
    ("In Python, what integer does print(2 ** 3 ** 2) output?", ["512"]),
    ("Tom is taller than Sam. Sam is taller than Bob. Who is the shortest?", ["bob"]),
    ("A rectangle has length 8 and width 3. What is its area?", ["24"]),
    ("If today is Monday, what day of the week is it 10 days from now?", ["thursday"]),
    ("How many minutes are there in 2.5 hours?", ["150"]),
    ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost, in dollars?", ["0.05", "5cents", ".05"]),
    ("What is the worst-case time complexity of binary search in Big-O notation?", ["ologn"]),
]


def norm(s):
    s = s.lower()
    s = re.sub(r"[\s$%(),*]", "", s)
    return s


def extract(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    m = list(re.finditer(r"answer\s*:?\s*", raw, flags=re.IGNORECASE))
    if m:
        return raw[m[-1].end():].strip().splitlines()[0] if raw[m[-1].end():].strip() else ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def probe_score(model, n=1024):
    ok = empties = 0
    detail = []
    for q, accept in PROBE:
        try:
            raw, dr = gen(model, q, n, system=PROBE_SYS, timeout=180)
        except Exception as e:
            raw, dr = f"<ERR {e}>", "err"
        ans = extract(raw)
        na = norm(ans)
        hit = any(norm(a) in na or na == norm(a) for a in accept) and na != ""
        if not raw.strip():
            empties += 1
        ok += hit
        detail.append(("+" if hit else "X", q[:40], ans[:24], dr))
    return ok, empties, detail


print("\n\n" + "=" * 64, flush=True)
print("PHASE 2 — reasoning probe (12 verifiable Qs, num_predict=1024)", flush=True)
print("=" * 64, flush=True)
PROBE_MODELS = [m.strip() for m in _ARGS.models.split(",") if m.strip()] or [
    "llama3.1:8b", "gemma4:e4b-it-qat",
    "gemma4:12b", "gemma4:26b-a4b-it-qat", "gemma4:31b-it-qat"]
probe_results = {}
for model in PROBE_MODELS:
    print(f"\nMODEL: {model}", flush=True)
    try:
        gen(model, "Say OK.", 32, timeout=120)
    except Exception as e:
        print(f"  warmup FAILED: {e}", flush=True)
    t0 = time.monotonic()
    ok, empties, detail = probe_score(model)
    probe_results[model] = (ok, empties)
    for mark, q, ans, dr in detail:
        print(f"    {mark} {q:<40} => {ans:<24} ({dr})", flush=True)
    print(f"  SCORE: {ok}/12 ({round(ok/12*100,1)}%)  empties={empties}  [{time.monotonic()-t0:.0f}s]", flush=True)
    unload(model)

print("\n\n" + "=" * 64, flush=True)
print("REASONING PROBE SUMMARY", flush=True)
print("=" * 64, flush=True)
for model in PROBE_MODELS:
    ok, empties = probe_results[model]
    print(f"  {model:<26} {ok:>2}/12 ({round(ok/12*100,1):>5}%)  empties={empties}", flush=True)
print("\nDONE", flush=True)
