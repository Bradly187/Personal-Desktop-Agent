# Daily Review — 2026-05-20

## Session Summary

Full-day engineering session. Covered market research, deep analysis of sensor priority fusion, four bug fixes, three sprint deliveries (A–C voice calibration, Sprint B iPad UI, Sprints 5–7), and systematic closure of the top 8 gaps from a post-analysis list.

---

## 1. Market Comparison

Conducted a structured competitive analysis against the full landscape of commercial accessibility desktop control systems:

| Product | Modalities | Flare-day adaptation | Local LLM | Price |
|---|---|---|---|---|
| Dragon Pro v16 | Voice only | ❌ | ✅ (perpetual) | $699 |
| Tobii PCEye 5 | Gaze only | ❌ | ❌ | ~$2,500 |
| Grid 3 | 5 sequential | ❌ | ❌ | ~$700 |
| Windows Voice Access | Voice only | Parkinson's only | Copilot+ only | Free |
| Apple Voice Control | Voice only | ❌ | ❌ | Free |
| Voiceitt | Atypical voice | ❌ (static model) | ❌ | Free/sub |
| **This system** | **7 simultaneous** | **✅ full** | **✅ always-local** | **$0** |

**Key finding:** No commercial product combines sensor fusion across more than 3 modalities simultaneously. No product has a flare-day model. No product uses a local LLM for command classification.

---

## 2. FusionEngine Deep Analysis + 4 Bug Fixes

Performed a line-by-line analysis of `fusion_engine.py`. Found 7 issues; fixed the 4 high/medium priority ones:

| Bug | Impact | Fix |
|---|---|---|
| Tilt/head starvation | Sub-dead-zone tilt permanently blocked voice/gesture | Moved `return` inside `if dx or dy:` |
| Gyro suppression starvation | UNCALIBRATED state blocked voice via early `return` | Wrapped pipeline in `if not _suppressed:` |
| Double cursor movement | Gaze-to-cursor (moveTo) + gaze-delta (moveRel) both fired same tick | `_apply_gaze_cursor` returns `bool`; gated `gaze_delta` on `not _gaze_cursor_moved` |
| Silent click drop | "click" keyword with no gaze target silently discarded | CLARIFY emitted in `else:` block |
| **Pain-day FusionConfig** | FusionEngine never read BehavioralTwinState pain_day_active | `apply_pain_day()` method; 6 thresholds relaxed; wired through coordinator |

24 new tests. All passing.

---

## 3. Sprint A — Acoustic Profiler (PC-side voice calibration)

New module: `acoustic_profiler.py`

- Measures RMS amplitude, spectral centroid, Whisper logprob per utterance
- Derives per-user `vad_threshold` (replaces static 0.015) and `logprob_floor` (replaces global -1.0)
- Scales both on flare days by `flare_vad_scale` (default 0.5 → voice at 50% baseline still works)
- Voice clarity as **Signal 5** in PainDayEngine (joins fail ratio, clarify ratio, gesture confidence, command rate)
- Passive calibration — builds profile silently during normal use, calibrated after 15 samples
- Drift detection: every 20 voice samples, checks if clarity dropped >30% → fires re-cal callbacks
- Seasonal prompt: every 50 commands, checks if >30 days since last calibration

6 new DB tables: `voice_calibration`, `voice_profile`, `voice_phrases`, `sensor_rom`, `flare_profile`, `ambient_transcripts`

---

## 4. Sprint B — iPad Accessibility Onboarding UI (4 new sheets)

OnboardingView expanded from 7 → 10 steps. Three new steps (all optional/skippable):

- **Step 7 — VoiceProfilingSheet:** 10 phrases × 3 repeats, 4s countdown. iPad streams mic while user reads each phrase. PC's AcousticProfiler captures samples passively.
- **Step 8 — GestureAssessmentSheet:** User rates each of 4 gestures (POINT/PINCH/OPEN_PALM/FIST) as Easy / Hard on bad days / Can't do this. Disabled gestures stored and synced to PC.
- **Step 9 — FlareProfileSheet:** Which sensors degrade, voice volume fraction slider (25/40/55/70% of baseline), manual pain day toggle (syncs to PC via WebSocket in <100ms).

Also: **QuickRecalSheet** — 3 phrases × 3 repeats (~90s). Shown automatically when PC detects voice drift or seasonal prompt fires. Wired into `ContentView` via `wsManager.recalibrationFeed`.

**Manual pain day sync chain:**
```
FlareProfileSheet toggle → SettingsStore.manualPainDay
  → SensorManager observer → WebSocketManager.sendPainDayOverride
  → ipad_bridge.py pain_day_override handler
  → BehavioralTwinState.set_manual_pain_day()
  → AcousticProfiler.get_vad_threshold(pain_day: true)
  → WhisperStream._silence_thresh relaxed immediately
```

---

## 5. Sprint C — Continuous Re-calibration

- After every 20 voice samples: `_compute_drift()` checks if clarity dropped ≥30% from baseline → fires `DriftResult` callback → `bridge.send_recalibration_request()` → `wsManager.recalibrationFeed` → `QuickRecalSheet`
- After every 50 commands: `on_any_command()` checks if >30 days since last calibration → seasonal prompt (same path, `reason="seasonal"`)
- `settings.lastCalibrationDate` written on completion; resets seasonal timer

---

## 6. Voice Pipeline Improvements

- **Wake phrase** `"hey agent"` / `"agent"` — punctuation-normalised (comma after "Hey" no longer breaks match)
- **Lecture mode** — `"hey agent start lecture mode"` stores non-command transcripts to `ambient_transcripts` table; `"hey agent stop lecture mode"` reverts to silent discard
- **Hallucination filter** — `no_speech_prob > 0.5` and `avg_logprob < -0.8` filters per segment before routing
- **CLARIFY echo suppression** — pre-suppress mic BEFORE TTS fires (estimated duration), post-suppress 1.5s for room echo tail; in-flight transcriptions checked against suppress window
- **Pending clarification context** — after CLARIFY, next voice command gets `[PENDING CLARIFICATION: ...]` prepended to LLM prompt so "up" resolves correctly
- **Awaiting clarification gate** — while waiting for clarification answer, responses >6 words are rejected (prevents lecture sentences answering)

---

## 7. Sprint 5 — Vision Grounding

`vision_grounder.py` — uses `claude-sonnet-4-6` vision to resolve named UI targets to pixel coordinates.

- Confidence gate: ≥0.7 required; below threshold falls through to Tesseract
- 2s cache per target name
- Fallback chain: vision → gaze_coords → Tesseract OCR → cursor + CLARIFY
- Hooked into `HybridCoordinator._execute_action` for CLICK with named target and no explicit coords

Expected CLICK success: 42% → ~78%

---

## 8. Sprint 6 — UIAutomation

`ui_automation.py` — Win32 UIAutomation BFS tree search for interactive elements.

- Fuzzy name scoring (exact match → contains → word overlap → value match)
- 0.3s timeout; 1s cache per (target, app)
- Targets VS Code, Chrome, Edge, Kiro, Windows Terminal, Notepad, Acrobat, Zotero
- Wired as first fallback in `_resolve_coords` before vision grounder
- Startup table check added

Expected CLICK success: ~78% → ~88%

---

## 9. Sprint 7 — Action Verification

`action_verifier.py` — Pillow perceptual diff pre/post screenshot.

- Verifies CLICK, OPEN, CLOSE, SCROLL (TYPE/HOTKEY skipped — no visual diff)
- 2% pixel change threshold = success
- 400ms delay after action for animations
- Pre-snapshot taken before dispatch; post-snapshot after; result included in execute() response
- Failed verifications log WARNING; result available for future BehavioralTwinState wiring

Expected CLICK success: ~88% → ~92%

---

## 10. Gap Closures (Top 8 from gap analysis)

| Rank | Gap | Closed |
|---|---|---|
| 1 | mediapipe not installed | ✅ `pip install mediapipe opencv-python` |
| 2 | GestureProcessor ignores `disabledGestures` | ✅ `set_disabled_gestures()` + `_debounced()` check |
| 3 | Sprint 6 UIAutomation | ✅ `ui_automation.py` |
| 4 | Sprint 7 Action Verification | ✅ `action_verifier.py` |
| 5 | Lecture notes not searchable | ✅ `search_lecture_notes()` + voice trigger |
| 6 | Acoustic profiler integration tests | ✅ 18 tests |
| 7 | Vision grounder tests | ✅ 11 tests |
| 8 | `_load_preference_model` WARNING on startup | ✅ Double-serialization fix in `log_settings_change` + safety parse |

---

## 11. Known-App Direct Launch

`_KNOWN_APPS` registry in `command_executor.py`:
- `"kiro"` / `"cairo"` / `"key row"` → `Kiro.exe` (direct subprocess, bypasses Win+S)
- `"vs code"` / `"vscode"` → `Code.exe`
- `"terminal"` / `"windows terminal"` → `wt.exe`

Voice corrections also applied **pre-gate** (before LLM sees text) so `"cairo"` → `"kiro"` always fires.

---

## 12. Test Count

| Suite | Before | After |
|---|---|---|
| pytest | 262 | 315 (+53) |
| Standalone integration | 30 | 31 |
| Swift XCTest | 15 | 15 |
| **Total** | **307** | **361** |

New tests: `test_fusion_fixes.py` (24), `test_acoustic_profiler.py` (18), `test_vision_grounder.py` (11).

---

## Open Items Carried Forward

| Item | Status |
|---|---|
| AgentCore deployment | 🔒 Blocked (bedrock-agentcore CLI broken) |
| VLLMInference activation | 🔒 Blocked (CUDA 13.x wheels not published) |
| Nemotron 340B RAM offload | 🔁 Stretch goal |
| Peace-jitter → BehavioralTwinState | 📋 Sprint 5+ |
| Sensor ROM assessment UI | 📋 Future sprint |
| Grad school study mode profile | 📋 Pre Jan 2027 |
| Worktree filesystem locks | 🔒 Other sessions; prune when they close |
| pix2tex handwriting OCR | 📦 Install when needed |

---

## Performance Snapshot (unchanged from 2026-05-15 baseline)

- Ollama llama3.1:8b warm p50: **373ms**
- Whisper large-v3 GPU load: **2.5s** (cached), **~4.2 GB VRAM**
- VRAM headroom: ~14 GB free for additional models
- Gate distribution: 91% bypass, 9% gate2_complexity (still too sparse for threshold tuning)

---

## Afternoon Session — App Store Privacy + Commercial Roadmap

### App Store Privacy Disclosure

Completed the full App Store privacy label for the DesktopAgent iPad app. Key decisions:

| Data Type | Usage Purpose | Notes |
|---|---|---|
| Audio Data | App Functionality | Voice commands → WhisperStream; Amazon Transcribe cloud fallback |
| Photos or Videos | App Functionality | Camera frames streamed to PC; ScreenshotStore |
| Hands | App Functionality | MediaPipe HandLandmarker for 13-gesture vocabulary |
| Head | App Functionality | ARKit face anchor for head pose + gaze tracking |
| Environment Scanning | App Functionality | LiDAR depth for gesture Z-validation |
| Other User Content | App Functionality | HandwritingCanvasView → pix2tex OCR |
| Product Interaction | App Functionality | Commands + routing logged to agent.db |
| Crash Data | App Functionality | Apple system crash reporting only |
| Health | **Not selected** | Pain-day inference is on-device gesture math, not health data |
| Location | **Not selected** | LiDAR is environment scan, not GPS |
| Sensitive Info | **Not selected** | RA accommodation is calibration parameter, not medical record |

Accessibility: **No** (VoiceOver/DynamicType/DarkMode not yet implemented — honest answer).
Content Rights: No third-party content.
Accessibility URL: Skipped (no public website).

---

### Commercialization Analysis

Full gap analysis conducted. Key findings:

- **Architecture decision:** Option A (iPad + Companion App) — ship a native Mac/Windows companion, not a cloud backend
- **Biggest blocker:** RTX 5090 hard dependency — must be replaced with cloud inference for general users
- **Cloud inference target:** Claude Haiku (`claude-haiku-4-5`) via direct API or Bedrock; $0.80/$4.00 per 1M tokens; estimated <$0.10/user/day
- **Mac-first:** companion app targets Mac before Windows; iPad + Mac is the likely user profile
- **No medical device claims:** market as "hands-free desktop control for people with limited hand mobility"
- **Pricing:** $9.99/month subscription via StoreKit; 14-day free trial; covers cloud inference cost

---

### 7-Phase Commercial Roadmap

| Phase | Dates | Mode | Goal |
|---|---|---|---|
| 1 — Hardening | May–Jul '26 | Full-time | 8hr session without restart |
| 2 — Coursework | Jul–Sep '26 | Full-time | Hands-free DB + OOP coursework |
| 3 — GPU removal | Sep–Nov '26 | Part-time | Works on MacBook M4 via Haiku |
| 4 — Companion app | Nov '26–Feb '27 | Part-time | 10-min install by non-technical user |
| 5 — Multi-user | Feb–Apr '27 | Part-time+ | 10 external beta users onboarded |
| 6 — Commercial infra | Apr–Jun '27 | Part-time | App Store submission accepted |
| 7 — Launch | Jul–Sep '27 | Flexible | 100 subscribers / $1K MRR |

Fall semester target: App stable for database class (SQL navigation, DBeaver) + OOP class (VS Code voice-to-code).

---

### Diagrams Generated

Three Mermaid diagrams rendered and saved to `docs/diagrams/` (PNG + SVG):

- **`domain-model`** — Class diagram: User/Subscription/Device/Session hierarchy + pipeline (FusionEngine → HybridCoordinator → ClaudeHaikuInference / OllamaInference → CommandExecutor)
- **`database-schema`** — ERD: 12 tables — 4 new commercial (USERS, SUBSCRIPTIONS, DEVICES, INFERENCE_COSTS) + 8 existing extended with user_id FK
- **`user-stories`** — Mindmap: 5 epics (Setup, Daily Control, Coding/Dev, Pain Day, Subscription)

