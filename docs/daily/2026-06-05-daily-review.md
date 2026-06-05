# Daily Review — 2026-06-05

*(Covers the 2026-06-04 → 06-05 push. The prior daily file was
`2026-06-03-daily-review.md`.)*

## Summary

A two-day tilt/cursor and coordinator-hardening sprint. Most of it landed on
`master` via PRs **#27–#31**; the final tranche — mouth-sound removal + magnetic
cursor gravity + a COM apartment-threading fix — is open as **PR #32**
(`fix/tilt-tap-click`), verified green and awaiting review/merge. Two of the three
open items carried from the 06-03 review are now resolved.

---

## Landed to `master` (PRs #27–#31)

### Coordinator hardening (#30) — closes two 06-03 open items ✅
- `fix(coordinator)`: **local-inference circuit-breaker** — `route()` now wraps the
  local call in `local_timeout_s` (default 15 s) → CLARIFY, so a hung local
  inference can't stall the pipeline. (Was the top open item in the 06-03 review.)
- `feat(coordinator)`: **LLM output schema validation** — `_parse_action` validates
  against `_VALID_COMMAND_VERBS`; a malformed verb degrades to CLARIFY instead of
  silently becoming a bad action. (Was the second open item.)
- `fix/refactor(approval)`: require an explicit confirmation word at the voice
  approval gate; unify DevAgent confirmation on a shared vocabulary.

### Tilt / cursor (#30, #31) ✅
- `feat(fusion)`: **absolute tilt spans the full virtual desktop** (multi-monitor —
  verified 7380×2880 at smoke).
- `fix(fusion)`: harden the tilt gyromouse — ratchet, 1€-filter guard, tremor clamp;
  drop non-finite tilt frames at ingress.
- `feat(ipad)`: easier tilt taps + OS-paired double-click; on-device **Tap
  Sensitivity** slider; tap-click pinned to the cursor + tap observability.

### Whisper perf (#30) ✅
- `perf(whisper)`: stop re-concatenating the full audio buffer every poll tick.
- `fix(whisper)`: isolate non-critical side-effects from the command path.

### Infra (#27, #29, #31) ✅
- Dependabot bumps (pip + uv groups, incl. aiohttp); `start_desktop.bat` now starts
  Ollama too; iPad declared encryption-exempt so TestFlight builds auto-clear
  export-compliance.

---

## Open — PR #32 (`fix/tilt-tap-click`, verified, awaiting merge)

5 commits, +1105 / −815.

### Mouth-sound control removed (PC + iPad)
The cluck/pop/hiss pipeline fired incidentally and was not a reliable control
surface. Deleted `SoundDetector.swift`, `SoundTrainingSheet.swift`, and the wiring
across SensorManager / Onboarding / SettingsStore / SensorActivityBar /
SensorDashboardView / FlareProfileSheet (Swift 41 → 40). PC-side, the `sound_action`
handler and the priority-2 sound branch are gone — **FusionEngine is now 6-level**
(was 7).

### Magnetic cursor gravity — re-enabled safely
The 2026-06-04 attempt destabilised the desktop two ways (a 30 Hz fullscreen Tk
overlay soft-hung the DWM compositor; a blind 3×/s whole-tree UIA walk spammed
E_POINTER on stale elements). The fix:
- `desktop/magnetic_overlay.py` **deleted** — gravity runs headless.
- `desktop/target_cache.py` `_loop` reworked: change-gated walk (foreground-hwnd +
  1.5 s heartbeat), consecutive-failure backoff (cap 2 s), `CoUninitialize` in
  `finally`.
- `desktop/ui_automation.py`: `collect_snap_targets_for_window(hwnd)` roots the BFS
  at `ElementFromHandle` (fresh, foreground-scoped); bounds 200 results / 0.3 s.
- `main.py`: starts the cache behind the `DA_CURSOR_GRAVITY` kill-switch (default on).
- `command_executor`: per-click UIA COM calls confined to a single CoInitialized(MTA)
  apartment thread (`_run_on_com_thread`) — fixes a latent cross-apartment pointer
  race (`RPC_E_WRONG_THREAD` / `E_POINTER`) from rotating `asyncio.to_thread` workers.

### Verification
- **684 pytest pass** (one live-network smoke, `test_remote_whisper_smoke.py`,
  excluded — it needs the laptop service).
- `main.py --safe-mode` ~105 s stability smoke: target_cache + cursor gravity active,
  FusionEngine 60 Hz multi-monitor, **0 E_POINTER / 0 tracebacks / 0 walk-failures**.

---

## Housekeeping (this review)
- `CLAUDE.md` updated: status header + new "Mouth-sound control removed" Done block;
  removed mouth-sound from the modality intro; **Sensor Priority 7 → 6** (renumbered);
  dropped `sound_action` from the WebSocket sensor-stream list and prose;
  `ipad_bridge` message-type count 26 → 25. (Cursor-gravity notes + the
  `target_cache.py` Key-Files row already landed in PR #32 commit `1bcfefe`.)
- Remote branches pruned to just `master` + `fix/tilt-tap-click` — the
  circuit-breaker / approval-gate / cluster / routing-experiment branches were
  merged or abandoned and cleaned up.

## Open Items (carried / new)
- **SVT fast-path for `ResourceGovernor`** (carried) — 5 s poll means up to 5 s
  before VRAM is released on an SVT attack; a `set_manual_pain_day(True)` callback
  hook would cut this to < 1 s.
- **iPad end-to-end magnetic verification** — the cursor-gravity stability smoke is
  green, but real-hardware confirmation (tilt cursor sticks to buttons) is still
  pending.
- **Memory index stale** — `MEMORY.md` still points at the 2026-05-11 superstate;
  predates gaze removal, AIOS alignment, the cluster tier, goal sessions, and the
  magnetic rework. A fresh superstate is worth writing. (`cursor_gravity.md` is
  current.)
- **`aios_sdk` package** (carried, low priority for a single-user system).
