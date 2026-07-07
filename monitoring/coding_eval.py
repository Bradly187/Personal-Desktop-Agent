"""HumanEval-style code-generation eval — execute-and-check, subprocess-sandboxed.

Each model is asked to implement a function; the generated code is extracted, the
problem's hidden assertions are appended, and the whole thing runs in a subprocess
with a hard timeout. Pass = subprocess exits 0. Failure is classified
(empty / syntax / assertion / runtime / timeout).

Gates the code/plan slot consolidation decision (gemma4 vs qwen3-coder:30b).
"""
import argparse
import json
import urllib.request
import re
import subprocess
import sys
import tempfile
import time
import os

OLLAMA = "http://localhost:11434/api/generate"
NUM_PREDICT = 4096          # generous: thinking trace + function body
GEN_TIMEOUT = 300           # per-request HTTP timeout
RUN_TIMEOUT = 10            # per-solution subprocess timeout
PY = sys.executable

_ap = argparse.ArgumentParser(description="HumanEval-style code-gen eval (execute-and-check).")
_ap.add_argument("--models", default="", help="comma-separated models (default: qwen3-coder + gemma lineup)")
_ARGS, _ = _ap.parse_known_args()
MODELS = [m.strip() for m in _ARGS.models.split(",") if m.strip()] or [
    "qwen3-coder:30b", "gemma3:27b", "gemma4:e4b-it-qat", "gemma4:12b", "gemma4:31b-it-qat"]

SYS = ("You are an expert Python programmer. Implement the requested function. "
       "Output ONLY a single Python code block containing the complete function "
       "definition — no explanation, no example usage.")

# (entry, prompt, tests)
PROBLEMS = [
    ("is_prime",
     "def is_prime(n): return True if n is a prime number, else False.",
     "assert is_prime(2) and is_prime(13) and is_prime(7919)\nassert not is_prime(1) and not is_prime(0) and not is_prime(15)"),
    ("fizzbuzz",
     "def fizzbuzz(n): return a list of strings for 1..n inclusive where multiples of 3 are 'Fizz', of 5 are 'Buzz', of both are 'FizzBuzz', else the number as a string.",
     "assert fizzbuzz(5) == ['1','2','Fizz','4','Buzz']\nassert fizzbuzz(15)[-1] == 'FizzBuzz'"),
    ("two_sum",
     "def two_sum(nums, target): return the two indices of the elements summing to target (each input has exactly one solution).",
     "assert sorted(two_sum([2,7,11,15],9)) == [0,1]\nassert sorted(two_sum([3,2,4],6)) == [1,2]"),
    ("is_palindrome",
     "def is_palindrome(s): return True if s is a palindrome considering only alphanumeric characters and ignoring case.",
     "assert is_palindrome('A man, a plan, a canal: Panama')\nassert not is_palindrome('race a car')\nassert is_palindrome('')"),
    ("gcd",
     "def gcd(a, b): return the greatest common divisor of non-negative integers a and b.",
     "assert gcd(48,18) == 6\nassert gcd(17,5) == 1\nassert gcd(0,5) == 5"),
    ("count_vowels",
     "def count_vowels(s): return the number of vowels (a,e,i,o,u, case-insensitive) in s.",
     "assert count_vowels('Hello World') == 3\nassert count_vowels('xyz') == 0"),
    ("reverse_words",
     "def reverse_words(s): return the words of s in reverse order, separated by single spaces, with no leading/trailing spaces.",
     "assert reverse_words('the sky is blue') == 'blue is sky the'\nassert reverse_words('  a   b c ') == 'c b a'"),
    ("max_subarray",
     "def max_subarray(nums): return the largest sum of any contiguous non-empty subarray of nums.",
     "assert max_subarray([-2,1,-3,4,-1,2,1,-5,4]) == 6\nassert max_subarray([-1,-2,-3]) == -1"),
    ("flatten",
     "def flatten(lst): return a single flat list with all nested lists fully flattened, preserving order.",
     "assert flatten([1,[2,[3,4]],5]) == [1,2,3,4,5]\nassert flatten([]) == []"),
    ("roman_to_int",
     "def roman_to_int(s): convert a Roman numeral string to its integer value.",
     "assert roman_to_int('III') == 3\nassert roman_to_int('LVIII') == 58\nassert roman_to_int('MCMXCIV') == 1994"),
]


def gen(model, prompt):
    body = json.dumps({
        "model": model, "prompt": f"{SYS}\n\n{prompt}", "stream": False,
        "options": {"temperature": 0, "num_predict": NUM_PREDICT},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=GEN_TIMEOUT).read())
    return d.get("response", ""), d.get("done_reason", "")


def unload(model):
    try:
        body = json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode()
        req = urllib.request.Request(OLLAMA, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def extract_code(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # fenced block first
    m = re.search(r"```(?:python)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # else: from first def/import to end
    m = re.search(r"((?:^|\n)\s*(?:import |from |def ).*)", raw, flags=re.DOTALL)
    return m.group(1).strip() if m else raw.strip()


def run_solution(code, entry, tests):
    if not code.strip():
        return "empty"
    if entry not in code:
        return "no-func"
    script = code + "\n\n" + tests + "\nprint('OK')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        # run_capped: generated code may spawn its own subprocesses; on timeout
        # kill the whole tree so a runaway grandchild can't outlive the eval.
        from core.proc_utils import run_capped
        r = run_capped([PY, path], capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.unlink(path); return "timeout"
    os.unlink(path)
    if r.returncode == 0 and "OK" in r.stdout:
        return "pass"
    err = (r.stderr or "").strip().splitlines()
    last = err[-1] if err else "?"
    if "AssertionError" in (r.stderr or ""):
        return "assert"
    if "SyntaxError" in (r.stderr or "") or "IndentationError" in (r.stderr or ""):
        return "syntax"
    return f"runtime:{last[:40]}"


results = {}
for model in MODELS:
    print(f"\n{'='*64}\nMODEL: {model}\n{'='*64}", flush=True)
    try:
        gen(model, "def _warm(): return 1")
    except Exception as e:
        print(f"  warmup FAILED: {e}", flush=True)
    passed = 0
    t0 = time.monotonic()
    for entry, prompt, tests in PROBLEMS:
        tq = time.monotonic()
        try:
            raw, dr = gen(model, prompt)
        except Exception as e:
            raw, dr = "", f"err:{e}"
        code = extract_code(raw)
        verdict = run_solution(code, entry, tests)
        ok = verdict == "pass"
        passed += ok
        print(f"  {'+' if ok else 'X'} {entry:<16} {verdict:<22} ({dr}, {time.monotonic()-tq:.0f}s)", flush=True)
    results[model] = (passed, time.monotonic() - t0)
    print(f"  SCORE: {passed}/{len(PROBLEMS)} ({round(passed/len(PROBLEMS)*100,1)}%)  [{time.monotonic()-t0:.0f}s]", flush=True)
    unload(model)

print(f"\n\n{'='*64}\nCODING-GENERATION EVAL SUMMARY\n{'='*64}", flush=True)
for model in MODELS:
    p, dt = results[model]
    print(f"  {model:<26} {p:>2}/{len(PROBLEMS)} ({round(p/len(PROBLEMS)*100,1):>5}%)  [{dt:.0f}s]", flush=True)
print("\nDONE", flush=True)
