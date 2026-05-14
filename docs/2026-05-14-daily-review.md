# Daily Review — 2026-05-14

Automated housekeeping run. No user present.

---

## Yesterday's Work (2026-05-13) — Summary

Exceptionally busy day: **20 commits** across three major themes.

### Theme 1 — TestFlight / CI pipeline (commits 48a4322 → 0a25b12)

Nine consecutive commits iterating on the GitHub Actions workflow to get TestFlight upload working from Windows CI:

| Attempt | What changed |
|---------|-------------|
| 48a4322 | Added signing workflow; sortable command grid; web client updates |
| d3f95fc | Fixed bundle ID typo: `bradtarber` → `bradtarver` in project.yml |
| 27e1a4a → 70262d3 | TestFlight auth: key newlines, `API_PRIVATE_KEYS_DIR`, key paths |
| bb1cd4e | Replaced altool with `apple-actions/upload-testflight-build@v1` |
| 224c07f → c476a47 | Tried iTMSTransporter, then `xcodebuild -exportArchive` with auth flags |
| a403356 | Fixed SIGNING_SETUP.md bundle ID typo |
| 8631f1e | Merged PR #1 (CI fixes branch) |
| a1cbe58 | Fix CI: bundle ID typo in workflow, xcodeVersion, Info.plist overwrite, invalid entitlement |
| 24f1f8b | Add ATS exception in Info.plist for local network WebSocket connections |
| 0a25b12 | Final: use `xcrun altool` instead of xcodebuild for TestFlight upload |

### Theme 2 — Audio streaming pipeline (commits d80aec2 → 1b4021a)

- **`d80aec2`** — Gap analysis: README updates, boto3 dependency, steering steering doc update
- **`ded0452`** — Model defaults updated: `llama3.1:8b` as primary for command/plan/general; `gpt-oss:20b` and `qwen3-vl:30b` dropped from primary profiles
- **`1b4021a`** — Audio streaming pipeline wired: `AudioStreamer` → WebSocket `audio_stream` messages → `WhisperStream` on PC; fixes `SoundDetector` DSPSplitComplex (Xcode 16.3 strict mode) and Combine `sink` binding

### Theme 3 — iPad App Hardening (commit 6fa5407)

The largest commit of the day: +1,769 lines, 25 files. All 13 tasks in `.kiro/specs/ipad-app-hardening/tasks.md` completed:

| Component | What was added |
|-----------|---------------|
| `Audio/SharedAudioSession.swift` | Reference-counted shared `AVAudioEngine` — eliminates 3-engine microphone conflict |
| `SensorManager.swift` | Lifecycle hub: instantiates all 6 sensors, wires Combine toggles, checks hardware availability |
| `Network/ServiceDiscovery.swift` | `NWBrowser` mDNS for `_desktop-agent._tcp`; 5-second fallback to manual IP |
| `ScreenshotStore.swift` | `@MainActor ObservableObject`; decodes base64 screenshots; sets `showScreenshot = true` |
| `UI/ScreenshotOverlayView.swift` | Full-screen overlay, dismiss-on-tap, accessibility annotations |
| `DesignSystem/DesignTokens.swift` | 80pt touch targets, spacing scale, radius scale, typography |
| `DesignSystem/AppTheme.swift` | Semantic color env, `adaptiveGlass()` with iOS 26 `.glassEffect()` fallback |
| `DesignSystem/Components/` | `DAButton`, `DACard`, `DASectionHeader`, `DAConnectionBanner` |
| `SettingsStore.wsURL` | Changed from force-unwrap `URL` to `URL?`; `wsURLOrDefault` safe fallback |
| `WebSocketManager` | States `.connecting` → `.connected` on first receive (not on resume) |
| `Tests/SettingsStoreURLTests.swift` | 205-line unit test suite fuzzing URL edge cases |
| All 5 views | Refactored to use DesignTokens sizing throughout |

**Also noteworthy:** `ipad_bridge.py` has an unstaged fix (not yet committed) — sends a `status` welcome message immediately on client connection so `WebSocketManager` can transition from `.connecting` to `.connected`. This is the companion PC-side fix required for hardening requirement 4.2.

---

## Housekeeping Performed Today

### Stale references fixed

| File | Location | Was | Now |
|------|----------|-----|-----|
| `.github/SIGNING_SETUP.md` | Line 49 | `com.bradtarber.DesktopAgent` | `com.bradtarver.DesktopAgent` |
| `CLAUDE.md` | Phase 2 iPad app line | Listed 10 original classes only | Updated to reflect full new structure: SensorManager, SharedAudioSession, ServiceDiscovery, ScreenshotStore, DesignSystem, all components |
| `.kiro/steering/structure.md` | iPadApp directory tree | Old flat Sensors/UI/Network | Expanded with Audio/, DesignSystem/, SensorManager.swift, ScreenshotStore.swift |
| `.kiro/steering/structure.md` | Persistent Files table | Legacy flat files (routing_log.jsonl, hotwords.txt, gesture_calibration.json, few_shot_memory.db) | Updated to reflect AgentDB + AnalyticsDB; legacy files noted as migrated |
| `.kiro/specs/ipad-sensor-focus/tasks.md` | Task 2.2 note | "mDNS discovery deferred to integration test phase" | "mDNS discovery implemented in iPad App Hardening spec (ServiceDiscovery.swift)" |

### Orphaned files cleaned up

| Action | File(s) |
|--------|---------|
| `git rm` staged | `.kiro/specs/wake-on-lan/.config.kiro`, `.kiro/specs/wake-on-lan/requirements.md` — directory deleted on disk but deletion was unstaged |

### Code issues noted (informational — not changed)

| File | Issue | Severity |
|------|-------|----------|
| `ipad_bridge.py` | Unstaged welcome-message fix — sends `status` on connect. Companion to hardening req 4.2. Should be committed. | Low |
| `local_inference.py:110` | `gpt-oss:20b` appears in benchmark table comment (accurate historical data, correctly marked 0% accuracy). Not a bug, just historical. | Informational |
| `hybrid_coordinator.py:168` | Transcribe fallback still a pass-through stub. Open work. | Low |
| `local_inference.py:174` | `VLLMInference` marked stub — matches open task 2.13. | Low |

### Untracked items noted (not staged — user decision)

| Path | Notes |
|------|-------|
| `.kiro/specs/ipad-app-hardening/` | New spec directory (requirements, design, tasks, config) — all 13 tasks marked complete. Should be committed. |
| `.kiro/hooks/run-tests.kiro.hook` | New kiro hook; user-triggered test runner. Should be committed. |
| `docs/Apple Account Recovery_Key.txt` | Contains sensitive account data — should **not** be committed; add to `.gitignore`. |
| `docs/AuthKey_3FC9HSSFRU.p8`, `docs/AuthKey_424DX7FJC2.p8` | App Store Connect API keys — should **not** be committed; already in docs/ which appears tracked. Verify `.gitignore` covers these. |
| `docs/IMG_0048.jpeg`, `docs/IMG_0049.jpeg` | Photo attachments — add to `.gitignore` if not needed in repo. |

---

## Current Task Completion (updated)

| Phase | Done | Total | Status |
|-------|------|-------|--------|
| 1 — Core pipeline | 7 | 7 | ✅ Complete |
| 2 — iPad sensors + integration | 11 | 13 | 🟡 2 blocked (gaze dwell needs Apple dev account) |
| 2 (PC) — vLLM evaluation | ~0.7 | 1 | 🟡 Ollama benchmarked; vLLM backend stub remains |
| 3 — LiDAR + gesture | 2 | 2 | ✅ Complete |
| 4 — Continuous training | 4 | 5 | 🟡 Needs soak time |
| iPad Hardening — all 9 reqs | 13 | 13 | ✅ Complete (new spec, finished yesterday) |
| CI / TestFlight | ~0.9 | 1 | 🟡 Upload method stabilized; monitor next build |

---

## Recommended Next Steps

1. **Commit `ipad_bridge.py`** welcome-message fix — it's the server-side companion to the hardening WebSocket state fix (req 4.2)
2. **Commit `.kiro/specs/ipad-app-hardening/`** and `.kiro/hooks/run-tests.kiro.hook` — untracked spec and hook ready for commit
3. **Add sensitive files to `.gitignore`** — `docs/Apple Account Recovery_Key.txt`, `docs/AuthKey_*.p8`, `docs/IMG_*.jpeg`, `docs/*.cer`, `docs/*.key`, `docs/*.p12`, `docs/*.mobileprovision`
4. **Task 2.13** — Implement full `VLLMInference` backend; validate streaming p95 <350 ms
5. **Task 3.5 (voice e2e)** — Stream audio from iPad → verify WhisperStream transcribes → FusionEngine routes correctly
6. **Confirm TestFlight build** — The `xcrun altool` approach from the last commit needs a live CI run to verify
7. **N.5 (soak analysis)** — After 1 week of usage, run gate threshold analysis on `agent.db`
