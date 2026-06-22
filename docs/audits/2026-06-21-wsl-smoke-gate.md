# WSL Smoke Gate — Task 6 of specs/wsl-terminal-routing

**Branch:** `feat/wsl-terminal-routing`  
**Date:** 2026-06-21  
**Gate owner:** Brad  
**Result:** GREEN — all 12 criteria passed, default flipped to ON

---

## Environment

- Host: Windows 11 Pro 10.0.26200 (RTX 5090 box)
- WSL distros: Ubuntu (target), NVIDIA-Workbench, docker-desktop
- bwrap: 0.9.0 at `/usr/bin/bwrap` (Ubuntu, installed as root)
- Script: `tests/smoke_wsl_gate.py`

---

## Prerequisites Completed

1. **bwrap installed** in Ubuntu WSL:
   ```
   wsl.exe -d Ubuntu -u root -e apt-get install -y bubblewrap
   # → bubblewrap 0.9.0-1ubuntu0.1 installed
   ```
2. **Config enabled** at `~/.claude/ipad_bridge/config.json`:
   ```json
   {"wsl_terminal_routing": {"enabled": true, "distro": "Ubuntu", "unknown_command_policy": "native"}}
   ```
   (After gate: default flipped to ON in `_wsl_routing_config()` — config no longer required.)

---

## Results

| Criterion | Spec ref | Result | Notes |
|-----------|----------|--------|-------|
| POSIX dev commands return `sandboxed=True` | R1.1 | PASS | git/pip/ls all JAILED; pytest rc=127 (not installed in WSL — correct) |
| Project dir appears as `/mnt/e/...` | R1.2 | PASS | `to_wsl_path` → `/mnt/e/Personal_Desktop_Agent` |
| PowerShell/`where` return `sandboxed=False` | R2.1 | PASS | Both NATIVE |
| Write to `/mnt/e/` root returns `Read-only file system` | R2.2 | PASS | `rc=2`, stderr: `cannot create /mnt/e/escape_test.txt: Read-only file system` |
| bwrap absent → NATIVE, no crash | R3.1 | PASS | Renamed bwrap.bak, `ls -la` fell back to NATIVE |
| Routing disabled → native path | R3.3 | PASS | `enabled: false` in config → `sandboxed=False` |

All 12 sub-criteria (4 commands × R1.1, plus R1.2, 2 × R2.1, 3 × R2.2, R3.1, R3.3) PASS.

---

## Post-Gate Actions

1. `specs/wsl-terminal-routing/requirements.md` Task 6 checkbox → `[x]`
2. `inference/sandbox.py` `_wsl_routing_config()` default flipped to `{"enabled": True, "distro": "Ubuntu", ...}`
3. `tests/smoke_wsl_gate.py` committed as a permanent manual smoke script
4. PR `feat/wsl-terminal-routing` → `master` opened

## Pending Integration (not blocking)

`feat/sandbox-interactive-hardening` (WS-1) adds `noninteractive_env()` + `inject_noninteractive_flags()`
to `run_sandboxed`. Thread those into `_maybe_run_wsl`'s `run_capped` call at merge time
(one-line `env=noninteractive_env()` addition — documented in the spec merge note and CLAUDE.md).
