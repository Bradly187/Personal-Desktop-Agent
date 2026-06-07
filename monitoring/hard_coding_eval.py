"""Phase 1 hard coding eval — LeetCode medium/hard + a debugging task.

Discriminating follow-up to coding_eval.py (which hit a ceiling: all models 10/10).
Reasoning coders run in their real think=true mode (clean answer in `response`).
Execute-and-check, subprocess-sandboxed with a hard timeout.
"""
import argparse, json, urllib.request, re, subprocess, sys, tempfile, time, os

OLLAMA = "http://localhost:11434/api/generate"
NUM_PREDICT = 6144
GEN_TIMEOUT = 600
RUN_TIMEOUT = 12
PY = sys.executable

_ap = argparse.ArgumentParser(description="Hard coding eval — LeetCode med/hard + debug task.")
_ap.add_argument("--models", default="", help="comma-separated models (default: qwen3-coder + gemma lineup)")
_ARGS, _ = _ap.parse_known_args()
MODELS = [m.strip() for m in _ARGS.models.split(",") if m.strip()] or [
    "qwen3-coder:30b", "gemma3:27b", "gemma4:e4b-it-qat", "gemma4:12b", "gemma4:31b-it-qat"]
# think=true is gemma4-specific (separate `thinking` field); qwen3-coder REJECTS it (HTTP 400)
# and runs best in its natural mode. Only gemma4 dense reasoning models use it. NOTE: Ollama
# reads `think` only as a TOP-LEVEL request param, NOT inside `options`.
THINK_MODELS = {m for m in MODELS if m.startswith("gemma4:") and "e4b" not in m}

SYS = ("You are an expert competitive programmer. Implement the requested function "
       "correctly and efficiently, handling all edge cases. Output ONLY a single "
       "Python code block with the complete function — no prose, no example usage.")

BUGGY = '''The following Python function should return the index of target in the sorted list nums, or -1 if absent, using binary search. It has a bug (it can loop forever). Return the CORRECTED function `def binary_search(nums, target):`.

```python
def binary_search(nums, target):
    lo, hi = 0, len(nums)
    while lo < hi:
        mid = (lo + hi) // 2
        if nums[mid] == target:
            return mid
        elif nums[mid] < target:
            lo = mid
        else:
            hi = mid
    return -1
```'''

PROBLEMS = [
    ("longest_palindrome",
     "def longest_palindrome(s): return the longest palindromic substring of s.",
     "assert longest_palindrome('babad') in ('bab','aba')\nassert longest_palindrome('cbbd')=='bb'\nassert longest_palindrome('a')=='a'\nassert longest_palindrome('forgeeksskeegfor')=='geeksskeeg'"),
    ("word_break",
     "def word_break(s, words): return True if s can be segmented into a space-separated sequence of one or more words from the list `words` (words reusable), else False.",
     "assert word_break('leetcode',['leet','code']) is True\nassert word_break('applepenapple',['apple','pen']) is True\nassert word_break('catsandog',['cats','dog','sand','and','cat']) is False"),
    ("coin_change",
     "def coin_change(coins, amount): return the fewest number of coins needed to make up amount, or -1 if impossible.",
     "assert coin_change([1,2,5],11)==3\nassert coin_change([2],3)==-1\nassert coin_change([1],0)==0"),
    ("edit_distance",
     "def edit_distance(a, b): return the Levenshtein edit distance between strings a and b.",
     "assert edit_distance('horse','ros')==3\nassert edit_distance('intention','execution')==5\nassert edit_distance('','abc')==3"),
    ("merge_intervals",
     "def merge_intervals(intervals): merge all overlapping intervals (list of [start,end]) and return the merged list sorted by start.",
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\nassert merge_intervals([[1,4],[4,5]])==[[1,5]]"),
    ("trap_rain_water",
     "def trap_rain_water(height): given a list of non-negative bar heights, return how much rain water can be trapped.",
     "assert trap_rain_water([0,1,0,2,1,0,1,3,2,1,2,1])==6\nassert trap_rain_water([4,2,0,3,2,5])==9\nassert trap_rain_water([])==0"),
    ("spiral_order",
     "def spiral_order(matrix): return all elements of the 2D matrix in spiral order (clockwise from top-left).",
     "assert spiral_order([[1,2,3],[4,5,6],[7,8,9]])==[1,2,3,6,9,8,7,4,5]\nassert spiral_order([[1,2,3,4],[5,6,7,8],[9,10,11,12]])==[1,2,3,4,8,12,11,10,9,5,6,7]"),
    ("min_window",
     "def min_window(s, t): return the minimum window substring of s containing all characters of t (with multiplicity), or '' if none.",
     "assert min_window('ADOBECODEBANC','ABC')=='BANC'\nassert min_window('a','a')=='a'\nassert min_window('a','aa')==''"),
    ("can_finish",
     "def can_finish(num_courses, prerequisites): given prerequisites as pairs [a,b] meaning b must come before a, return True if all courses can be finished (no cycle).",
     "assert can_finish(2,[[1,0]]) is True\nassert can_finish(2,[[1,0],[0,1]]) is False\nassert can_finish(3,[[1,0],[2,1]]) is True"),
    ("binary_search",
     BUGGY,
     "assert binary_search([1,2,3,4,5],3)==2\nassert binary_search([1,2,3,4,5],1)==0\nassert binary_search([1,2,3,4,5],5)==4\nassert binary_search([1,2,3,4,5],6)==-1\nassert binary_search([],1)==-1\nassert binary_search([1,3,5,7,9],7)==3"),
]


def gen(model, prompt):
    payload = {"model": model, "prompt": f"{SYS}\n\n{prompt}", "stream": False,
               "options": {"temperature": 0, "num_predict": NUM_PREDICT}}
    if model in THINK_MODELS:
        payload["think"] = True
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=GEN_TIMEOUT).read())
    return d.get("response", ""), d.get("done_reason", "")


def unload(model):
    try:
        req = urllib.request.Request(OLLAMA, method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode())
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def extract_code(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    m = re.search(r"```(?:python)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"((?:^|\n)\s*(?:import |from |def ).*)", raw, flags=re.DOTALL)
    return m.group(1).strip() if m else raw.strip()


def run_solution(code, entry, tests):
    if not code.strip():
        return "empty"
    if f"def {entry}" not in code:
        return "no-func"
    script = code + "\n\n" + tests + "\nprint('OK')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script); path = f.name
    try:
        r = subprocess.run([PY, path], capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.unlink(path); return "timeout"
    os.unlink(path)
    if r.returncode == 0 and "OK" in r.stdout:
        return "pass"
    se = r.stderr or ""
    if "AssertionError" in se:
        return "assert"
    if "SyntaxError" in se or "IndentationError" in se:
        return "syntax"
    last = se.strip().splitlines()[-1] if se.strip() else "?"
    return f"runtime:{last[:36]}"


results = {}
for model in MODELS:
    print(f"\n{'='*64}\nMODEL: {model}  (think={model in THINK_MODELS})\n{'='*64}", flush=True)
    try:
        gen(model, "def _warm(): return 1")
    except Exception as e:
        print(f"  warmup FAILED: {e}", flush=True)
    passed = 0; t0 = time.monotonic()
    for entry, prompt, tests in PROBLEMS:
        tq = time.monotonic()
        try:
            raw, dr = gen(model, prompt)
        except Exception as e:
            raw, dr = "", f"err:{str(e)[:30]}"
        verdict = run_solution(extract_code(raw), entry, tests)
        ok = verdict == "pass"; passed += ok
        print(f"  {'+' if ok else 'X'} {entry:<20} {verdict:<24} ({dr}, {time.monotonic()-tq:.0f}s)", flush=True)
    results[model] = (passed, time.monotonic() - t0)
    print(f"  SCORE: {passed}/{len(PROBLEMS)} ({round(passed/len(PROBLEMS)*100,1)}%)  [{time.monotonic()-t0:.0f}s]", flush=True)
    unload(model)

print(f"\n\n{'='*64}\nHARD CODING EVAL SUMMARY\n{'='*64}", flush=True)
for model in MODELS:
    p, dt = results[model]
    print(f"  {model:<26} {p:>2}/{len(PROBLEMS)} ({round(p/len(PROBLEMS)*100,1):>5}%)  [{dt:.0f}s]", flush=True)
print("\nDONE", flush=True)
