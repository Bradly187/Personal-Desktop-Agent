# Spec: Remove Chatterbox TTS Backend

**Spec ID:** `chatterbox-removal`
**Status:** Completed (2026-07-01)
**Date:** 2026-07-01
**Branch:** `chore/remove-chatterbox-tts`
**Origin:** Antigravity handoff spec, executed by Claude Code; approved via user handoff.
**Decision:** [D019](../../docs/decisions.md#d019)

---

## Motivation

Chatterbox was added in May 2026 to provide a local GPU TTS option with zero-shot
voice cloning. By June 2026, Kokoro (local ONNX) became the default backend and
covers the same core value proposition — fully offline, zero cloud cost, local
GPU-upgradeable — without Chatterbox's dependency problems.

| Problem | Detail |
|---------|--------|
| `torch==2.6.0` pin conflict | Chatterbox hard-pins an older torch; the rest of the stack is on `torch 2.12.0`. Blocked whole-file `requirements.txt` install (PR #125) — that's why it was moved to "install-separately". |
| ~6–8 GB VRAM residency | Model stays in VRAM on first load, competing with specialist LLMs and Whisper. |
| Dead feature path | `tts_backend: "chatterbox"` appears in zero documented sessions; no `agent.db` routing-log usage. `chatterbox_voice_ref` has been `null` since initial config. |
| Kokoro already covers it | Local, zero-egress, no VRAM hold (CPU by default, GPU opt-in), no dependency conflict. |

## What Did NOT Change

- **Kokoro** remains the default runtime backend.
- **Polly** remains hardcoded in `approval_hook.py` (approval gate unchanged).
- **SAPI** remains the guaranteed-available fallback.
- `tts_backend` in `approval_config.json` stays; valid values are now `kokoro | polly | sapi`.

## Behavior change

`polly_stream.get_client()` now logs a WARNING and falls back to Polly for any
unknown `tts_backend` value (e.g. a stale `"chatterbox"` config) — the
degradation is visible in logs instead of silent.

## Change inventory

- **Deleted:** `tts/chatterbox_tts.py`
- **Code:** `tts/polly_stream.py` (dispatch branch + singleton removed; unknown-backend warning added), `tts/sapi_tts.py` (doc comment)
- **Config:** `approval_config.json` (3 `chatterbox_*` keys removed)
- **Requirements:** `requirements.txt`, `requirements-linux.txt`, `requirements-wsl.txt`, `setup_wsl_deps.sh`
- **Docs:** `CLAUDE.md`, `docs/tts.md`, `README.md`, `CODE_ANALYSIS.md`, `docs/file-map.md`, `docs/architecture/desktop-agent-overview.md`, `specs/steering/tech.md`, `specs/ipad-sensor-focus/diagrams/01-system-architecture.md`, `specs/ipad-sensor-focus/diagrams/15-sprint-roadmap.md`
- Historical docs (daily reviews, CHANGELOG, audits, cost_savings_plan, JUNE_2026_ROADMAP) intentionally untouched.

## Verification (Gate 2)

1. `tts/chatterbox_tts.py` gone.
2. Full pytest suite — zero new failures.
3. Kokoro smoke test via `get_client()`.
4. No `chatterbox` matches remain in `*.py`.
5. `get_client(backend="chatterbox")` degrades to Polly with a logged warning, no crash.
