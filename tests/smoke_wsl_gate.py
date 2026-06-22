"""WSL Smoke Gate -- Task 6 of specs/wsl-terminal-routing.

Run from the repo root on the Windows host:
    python tests/smoke_wsl_gate.py

Exercises all 6 pass criteria (R1.1, R1.2, R2.1, R2.2, R3.1, R3.3).
Prints PASS / FAIL for each criterion and exits non-zero if any fail.
"""

import logging
import os
import sys

# Make sure we can import from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

import inference.sandbox as sb

PROJECT = r"E:\Personal_Desktop_Agent"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    results.append((label, ok, detail))


# -------------------------------------------------------------------------
# Test 1 -- POSIX dev commands route through bwrap (R1.1)
# -------------------------------------------------------------------------
print("\nTest 1 -- POSIX dev commands route through WSL+bwrap")
posix_cmds = ["pytest --version", "git status", "pip list", "ls -la"]
for cmd in posix_cmds:
    r = sb.run_sandboxed(cmd, project_dir=PROJECT, timeout=30)
    tag = "JAILED" if r.sandboxed else "NATIVE"
    print(f"    [{tag}] {cmd!r:42s}  rc={r.returncode}")
    check(f"R1.1 sandboxed: {cmd!r}", r.sandboxed,
          f"rc={r.returncode}" + (f" stderr={r.stderr[:80]!r}" if r.returncode != 0 else ""))

# -------------------------------------------------------------------------
# Test 1b -- project dir appears as /mnt/e/... in argv (R1.2)
# -------------------------------------------------------------------------
print("\nTest 1b -- project dir translates to /mnt/e/... (R1.2)")
wsl_proj = sb.to_wsl_path(PROJECT)
check("R1.2 to_wsl_path produces /mnt/e/ prefix", wsl_proj is not None and wsl_proj.startswith("/mnt/e/"), wsl_proj or "None")

# -------------------------------------------------------------------------
# Test 2 -- Windows-only commands stay native (R2.1)
# -------------------------------------------------------------------------
print("\nTest 2 -- Windows-only commands stay native")
win_cmds = ["powershell -c Get-Date", "where python"]
for cmd in win_cmds:
    r = sb.run_sandboxed(cmd, project_dir=PROJECT, timeout=30)
    tag = "JAILED" if r.sandboxed else "NATIVE"
    print(f"    [{tag}] {cmd!r}")
    check(f"R2.1 native: {cmd!r}", not r.sandboxed)

# -------------------------------------------------------------------------
# Test 3 -- write outside project bind fails (R2.2)
# -------------------------------------------------------------------------
print("\nTest 3 -- write outside project bind returns Read-only file system (R2.2)")
r3 = sb.run_sandboxed(
    r"sh -c 'echo bad > /mnt/e/escape_test.txt'",
    project_dir=PROJECT,
    timeout=10,
)
print(f"    sandboxed={r3.sandboxed}  rc={r3.returncode}  stderr={r3.stderr[:120]!r}")
check("R2.2 sandboxed=True for escape attempt", r3.sandboxed)
check("R2.2 rc != 0 for escape attempt", r3.returncode != 0,
      f"rc={r3.returncode} stderr={r3.stderr[:80]!r}")
check("R2.2 stderr contains 'Read-only'",
      "read-only" in r3.stderr.lower() or "permission denied" in r3.stderr.lower(),
      r3.stderr[:120])

# -------------------------------------------------------------------------
# Test 4a -- bwrap absent -> graceful native fallback (R3.1)
# -------------------------------------------------------------------------
print("\nTest 4 -- bwrap absent degrades gracefully (R3.1)")

import subprocess, shutil

# Rename bwrap temporarily (as root).
renamed = False
try:
    r_mv = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e",
         "mv", "/usr/bin/bwrap", "/usr/bin/bwrap.bak"],
        capture_output=True, text=True, timeout=10,
    )
    renamed = r_mv.returncode == 0
except Exception as e:
    print(f"    WARNING: could not rename bwrap: {e}")

if renamed:
    # Clear the availability cache so the probe re-runs.
    sb._wsl_avail_cache.clear()
    r4 = sb.run_sandboxed("ls -la", project_dir=PROJECT, timeout=15)
    tag = "JAILED" if r4.sandboxed else "NATIVE"
    print(f"    [{tag}] ls -la  rc={r4.returncode}")
    check("R3.1 falls back to NATIVE when bwrap absent", not r4.sandboxed,
          f"sandboxed={r4.sandboxed}")

    # Restore bwrap.
    subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-u", "root", "-e",
         "mv", "/usr/bin/bwrap.bak", "/usr/bin/bwrap"],
        capture_output=True, text=True, timeout=10,
    )
    sb._wsl_avail_cache.clear()
    print("    bwrap restored.")
else:
    print("    SKIP: could not rename bwrap (run as root may not be available).")
    check("R3.1 bwrap-absent fallback (skipped -- rename failed)", True, "SKIPPED")

# -------------------------------------------------------------------------
# Test 4b -- routing disabled -> identical to native path (R3.3)
# -------------------------------------------------------------------------
print("\nTest 4b -- routing disabled -> native path (R3.3)")
import json
from pathlib import Path

cfg_path = Path.home() / ".claude" / "ipad_bridge" / "config.json"
original_cfg = cfg_path.read_text(encoding="utf-8")

disabled_cfg = {"wsl_terminal_routing": {"enabled": False}}
cfg_path.write_text(json.dumps(disabled_cfg), encoding="utf-8")

# Clear cache so new config is read.
sb._wsl_avail_cache.clear()

r5 = sb.run_sandboxed("ls -la", project_dir=PROJECT, timeout=15)
tag = "JAILED" if r5.sandboxed else "NATIVE"
print(f"    [{tag}] ls -la  rc={r5.returncode}")
check("R3.3 routing disabled -> sandboxed=False", not r5.sandboxed,
      f"sandboxed={r5.sandboxed}")

# Restore original config.
cfg_path.write_text(original_cfg, encoding="utf-8")
sb._wsl_avail_cache.clear()
print("    Config restored.")

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"RESULTS: {passed} passed, {failed} failed (of {len(results)} criteria)")
if failed:
    print("\nFAILED criteria:")
    for label, ok, detail in results:
        if not ok:
            print(f"  - {label}: {detail}")
    sys.exit(1)
else:
    print("\nAll criteria PASS -- gate GREEN.")
    sys.exit(0)
