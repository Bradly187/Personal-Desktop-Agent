# Daily Review — 2026-05-16

Automated housekeeping run. No user present.

---

## Yesterday's Work (2026-05-15) — Summary

Extremely productive day: **13 commits** across four major themes.

### Theme 1 — Polly TTS integration (commits e9a980e → c672da8)

Initial Polly TTS wired into `command_executor.py` and `hybrid_coordinator.py`, followed immediately by a full bidirectional streaming upgrade:

| Commit | What changed |
|--------|-------------|
| e9a980e | Polly TTS `_polly_speak()` in `command_executor.py`; CLARIFY action now speaks aloud via Neural 16 kHz PCM; 3 integration tests added |
| b359a4d | Bug sweep: `sd.get_stream()` None guard, `SEARCH_WEB` URL encoding, `_retranscribe()` logic fix |
| e13ea00 | **Bidirectional streaming upgrade**: `polly_stream.py` + `tts_service/server.js` — Node.js sidecar calls `StartSpeechSynthesisStream` (Generative engine, OGG Vorbis); Python client (`PollyStreamClient`) wraps sidecar with `speak_sync()`, `speak()`, `speak_stream()`, `cancel()`; auto-start and npm-install on first use; `dev_agent.py` and `fusion_engine.py` wired to use new client |
| c672da8 | **Voice sweep**: all five TTS paths switched from Gregory/Joanna/Ruth to Danielle (only en-US voice supporting both Generative + Long-form engines) |
| ba6e97a | CLAUDE.md updated with full Polly voice table and TTS path documentation |

### Theme 2 — Voice approval gate (commits 00bea77 → 94fcd38)

Complete voice-gated approval flow for Claude Code hooks:

| Commit | What changed |
|--------|-------------|
| 00bea77 | `approval_hook.py` + `approval_config.json` — Polly speaks action, records mic, Whisper-tiny transcribes, yes/no exits 0/2 |
| e92eb83 | iPad mic integration via `~/.claude/approval/pending` + `response` signal files; `whisper_stream.py` intercepts next utterance when pending marker exists |
| 94fcd38 | Concise spoken messages — folder name only, not full filepath |

### Theme 3 — Chatterbox local TTS (uncommitted working changes)

New local TTS backend for offline/low-latency use on the RTX 5090:

- `chatterbox_tts.py` — `ChatterboxClient` class mirroring `PollyStreamClient` interface; emotion exaggeration (0.25–2.0), paralinguistic tags, zero-shot voice cloning via audio prompt
- `polly_stream.get_client()` extended to dispatch to chatterbox when `tts_backend == "chatterbox"` in `approval_config.json`
- `approval_config.json` — chatterbox config keys added (`tts_backend`, `chatterbox_exaggeration`, `chatterbox_cfg_weight`, `chatterbox_voice_ref`)
- `requirements.txt` — `chatterbox-tts` added

### Theme 4 — iPad overlay touch-debug fix (commit 1efc7f4)

Full fix for SwiftUI overlay blocking tab bar and content taps:

- `DwellToolbarContainer.swift` — outer ZStack `.allowsHitTesting(false)` with toolbar `.allowsHitTesting(true)` (was blocking entire screen)
- `DAConnectionBanner.swift` — `.allowsHitTesting(isDisconnected)` so banner only intercepts taps when visibly showing
- 2 new Swift XCTest files: `OverlayTouchInterceptionTests.swift` (bug geometry tests) + `OverlayPreservationTests.swift` (17 preservation properties)
- `.kiro/specs/ipad-ui-touch-debug/` — spec directory added with bugfix.md, design.md, tasks.md

### New untracked files

| File | Purpose |
|------|---------|
| `chatterbox_tts.py` | Local TTS backend (RTX 5090, needs `chatterbox-tts` pip package) |
| `health_viz.py` | Cosmic nebula system health visualization (CPU/GPU/VRAM drive particle density and color) |
| `health_viz_icon.ico` / `.png` | Icons for health_viz window |
| `start_agent.bat` | Windows startup script — launches `main.py`, logs to `logs/agent_startup.log` |

---

## Housekeeping Performed Today (2026-05-16)

### Bugs fixed

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `approval_hook.py` | 9 | Docstring said "Gregory neural" — stale voice after voice sweep | Changed to "Danielle neural" |
| `approval_hook.py` | 38 | `log.debug(...)` called but `log` never defined — `NameError` at runtime when iPad approval times out and falls back to PC mic | Added `import logging` + `log = logging.getLogger(__name__)` after numpy import |
| `command_executor.py` | 92 | `sd.get_stream().active` — no None guard; raises `AttributeError` if stream hasn't started or already finished | Fixed to `sd.get_stream() and sd.get_stream().active` (matches pattern in `polly_stream.py` and `approval_hook.py`) |
| `tts_service/server.js` | 70 | JSDoc `@param` comment said "default Ruth" — stale after voice sweep | Changed to "default Danielle" |

### No stale file paths or broken imports found

- All cross-file imports (`from polly_stream import get_client`, `from chatterbox_tts import ChatterboxClient`) resolve correctly to existing classes.
- `main.py` contains no stale voice references.
- Worktree at `.claude/worktrees/lucid-chatelet-524921/` still holds pre-sweep Gregory/Joanna references — these are isolated to the worktree and do not affect the active branch.

### Open items (not actioned — user decision required)

| Item | Notes |
|------|-------|
| `chatterbox_tts.py`, `health_viz.py`, `start_agent.bat`, icons are untracked | Consider committing or adding to `.gitignore` |
| `chatterbox-tts` in `requirements.txt` has no pinned version | Pin once the package stabilises |
| `chatterbox_tts.py` VRAM usage unquantified | CLAUDE.md notes ~6–8 GB estimated; run `--measure-vram` once installed to confirm alongside Whisper |

---

## Test count (as of 2026-05-16)

198 pytest tests + 30 standalone integration scripts + 6 Swift XCTest files = **234 total**
