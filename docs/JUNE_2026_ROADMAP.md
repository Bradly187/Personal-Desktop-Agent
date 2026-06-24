# Personal Desktop Agent — Architecture, Status & June 2026 Roadmap

*Authored 2026-05-30. Covers commercial Phase 1 (Hardening, May–Jul '26).*

> **⚠️ Historical snapshot — read with CLAUDE.md as the current source of truth (banner added 2026-06-24).**
> The Part 1 architecture analysis below predates several large changes:
> - The **laptop compute cluster was excised** (#118) — the agent is **single-machine local-only**, not a two-machine split.
> - **Eye-gaze / head-pose / mouth-sound control were removed** — FusionEngine priority is now **6-level**, not 10.
> - Message-type counts are stale (now **26 iPad→PC**, **12 PC→iPad** — see `docs/websocket-protocol.md`).
> - Several Part 2 hardening items have **shipped**: B1 (60 Hz `pyautogui.position()` now cached at 10 Hz via `_cursor_cache_loop`), B4 (destructive git verbs now gated through `_confirm_destructive_op`), C4 (`websockets` pinned to `==16.0`). Genuinely-open items remain: B2 (8-hour soak), A1 (RealSense L515 integration), C2 (speculative decoding).

---

## Part 1 — Architecture Analysis

### 1.1 The two-machine split

```
┌─────────────────────────┐         WebSocket :8765         ┌──────────────────────────────────────┐
│  iPad (Swift/SwiftUI)    │  ───────────────────────────▶  │  Windows PC · RTX 5090 (Python async)  │
│  Sensor hub + touch UI   │   28 msg types iPad→PC          │  Inference + desktop execution         │
│  62 Swift source files   │  ◀───────────────────────────  │  118 Python modules                    │
└─────────────────────────┘   5 msg types PC→iPad           └──────────────────────────────────────┘
```

The iPad is the sensor/UI surface; the PC does all inference and all desktop actuation. Every
pipeline boundary carries a single `Command` dataclass DTO — never raw dicts.

### 1.2 Data plane (sensor → action)

```
iPad sensors → ipad_bridge (:8765) → FusionEngine (60 Hz, 10-level priority) → HybridCoordinator
                                                                                       │
                                          ┌────────────────────────────────────────────┤
                                          │ Gate 0 privacy → Gate 1 confidence →         │
                                          │ Gate 2 complexity → Gate 3 VRAM → Gate 4 latency
                                          │                                              │
                                   DomainClassifier (keyword scorer)                     │
                                    ┌──────────┴───────────┐                             │
                              "command" domain        dev domains                        │
                              llama3.1:8b (verb)   (code/math/vision/plan/general)        │
                                    │              VLLMSpecialistPool / CloudDevAgent      │
                                    └──────────┬───────────┘                             │
                                          CommandExecutor (16 verbs)                      │
                                          → mcp_server/tools → pyautogui / Win32 ◀────────┘
```

**FusionEngine** is the scheduler: 7 priority levels (touch > sound > voice-click > tilt > gesture >
local-keyword > Whisper), with starvation fall-through so a high-priority-but-idle source never
blocks a lower one. Touch/sound bypass the LLM entirely (deterministic-first). *(Gaze/head removed
2026-05-30.)*

**HybridCoordinator** is the router: 4 gates (+Gate 0 privacy pre-filter) decide local vs. AWS
Bedrock cloud fallback. It also intercepts dev-domain queries *before* the gates and forwards them
to DevAgent (now optionally to the cloud — see §2.4).

### 1.3 Inference plane

| Path | Trigger | Model | Backend |
|------|---------|-------|---------|
| Command | simple desktop verbs | `llama3.1:8b` (4.6 GB) | Ollama (default) / vLLM |
| Dev — local | code/math/vision/plan/general | `gemma4:31b` AWQ (one shared instance) | `VLLMSpecialistPool` (destroy-recreate, ~50 s wake) |
| Dev — cloud | same, when GPU busy/disabled | `claude-sonnet-4-6` | **`CloudDevAgent`** (Anthropic Messages API) — *new this session* |
| Cloud fallback | Gate 2/3/4 fail | `claude-haiku-4-5` | raw Bedrock |

The 30B specialist and the command engine **cannot co-reside** with Whisper on 32 GB VRAM, so the
pool tears the command engine down to load a specialist and vice-versa (Blackwell CuMem conflict
forces destroy-recreate, not sleep). This ~50 s GPU thrash is exactly what `CloudDevAgent` and the
commercial "GPU removal" phase are designed to eliminate.

### 1.4 The 9 packages (post-reorg, committed in `f52d58f`)

`core/` (bridge, fusion, coordinator, executor) · `inference/` (local_inference, model_router,
dev_agent, **cloud_dev_agent**, kiro_client, codebase_indexer) · `sensors/` (whisper, gesture,
lidar, one_euro, viewer) · `adaptive/` (behavioral_twin_state, continuous_trainer, content_filter,
trust_classifier) · `calibration/` (acoustic_profiler, voice_calibrator, gyro_bias) · `desktop/`
(vision_grounder, ui_automation, action_verifier,
flick_engine, snap_zones) · `storage/` (db, audit_log, semantic_memory, session_analyzer) ·
`monitoring/` (metrics, dashboard) · `tts/` (polly_stream, chatterbox_tts).

### 1.5 Cross-cutting systems

- **Adaptation:** `BehavioralTwinState` (PainDayEngine, 5 signals incl. voice clarity) relaxes
  thresholds on RA flare days; `ContinuousTrainer` calibrates routing thresholds, few-shot ranking,
  and gesture velocity floors (p10 observed, −30 % on pain days).
- **Calibration:** per-user VAD/logprob (`AcousticProfiler`), 4-condition voice profiles
  (`VoiceCalibrator`). (Gaze monitor calibration removed 2026-05-30.)
- **Storage:** `AgentDB` (aiosqlite, 27 tables) + `AnalyticsDB` (DuckDB) + `audit.db` (append-only,
  trigger-locked) + ChromaDB semantic memory.
- **Observability:** in-process `Metrics` singleton, VRAM poller, optional `/metrics` endpoint,
  curses dashboard, post-session DuckDB analytics.

### 1.6 Architectural assessment

**Strengths:** clean ABC seams (`LocalInference`, swappable backends); deterministic-first routing;
graceful degradation everywhere (every sensor wraps hardware imports in try/except); a real
user-state model feeding the scheduler. This is a genuinely novel "personal AIOS" design.

**Risks / debt:**
- **Sensor assumptions don't match hardware** (the central issue — Part 3).
- VRAM is the binding constraint; the command↔specialist 50 s thrash is a UX cliff on any dev query.
- AI-experiment layers (routing classifier, gesture model, review-RAG) are **data-starved**, not
  built — they need real logged usage, which the hardware gap currently prevents generating.
- Test depth is high (~557 pytest) but is mostly unit/property; there is **no long-running soak
  evidence** yet for the 8-hour-session commercial milestone.

---

## Part 2 — Current Status (2026-05-30)

### 2.1 Delivered

- **Spec:** 68/68 tasks complete (Phases 1–7, Sprints A–H, G1–G5); only AgentCore (6.4) deferred &
  source-deleted. ROADMAP Phase-7 items #1–#8, #10 done; **#9 speculative decoding pending**.
- **Tests:** ~557 pytest + 31 integration scripts + 15 Swift XCTest.
- **vLLM:** 0.21.0 + torch 2.11.0+cu128 **verified working** in WSL2 on the RTX 5090 (2026-05-29).
- **Cloud dev path:** `CloudDevAgent` added this session (Anthropic Messages API, 13 tests passing).

### 2.2 What actually RUNS vs. what is DORMANT  ⚠️

This is the single most important status fact and it is **not** reflected in CLAUDE.md or the spec.

| Pipeline | Designed for | Reality on the hardware on hand |
|----------|-------------|---------------------------------|
| Touch, trackpad, command pad, handwriting | any iPad | ✅ works |
| Tilt / gyro navigation | Core Motion | ✅ works |
| Voice (Whisper, keyword, sound actions) | any iPad mic | ✅ works |
| 2D hand landmarks | camera | ✅ works (camera frame only) |
| **Gaze tracking** | TrueDepth + ARKit face anchor | ❌ **removed 2026-05-30** — was dormant (`isSupported == false`) |
| **Head pose** | TrueDepth + ARKit | ❌ **removed 2026-05-30** — was dormant |
| **LiDAR depth / gesture depth validation** | iPad Pro LiDAR | ❌ **dormant** — code compiles, produces no data |

The device on hand is a **standard iPad with a home button — no LiDAR, no TrueDepth**. So gaze,
head-pose, and all depth-validated gesture work (Sprints G1–G5, flick physics, the whole
gaze-monitor-calibration effort) are **built but cannot run**. They are validated only by unit tests
with synthetic inputs.

### 2.3 Incoming hardware (the June unblock)

**Intel RealSense L515** (~$200, purchase target **~2026-05-31**) — solid-state LiDAR, 0.25–9 m,
best accuracy at 0.3–1 m gesture range, RGB + depth over USB to the PC. Memory note:
*"After purchase, `realsense_receiver.py` is the first integration task."* It does **not** exist yet.

### 2.4 Uncommitted work (commit hygiene needed)

`git status` shows 4 modified + 3 untracked, mixing **two unrelated efforts**:
- **Mine (this session):** `cloud_dev_agent.py`, `test_cloud_dev_agent.py`, plus edits to
  `hybrid_coordinator.py`, `main.py`, `requirements.txt`.
- **Pre-existing WIP (not mine):** `inference/local_inference.py` (+223 lines — looks like the
  `set_pre_wake_hook` for `VLLMInference` that `main.py` already references) and `scripts/`.

These should be split into ≥2 clean commits before more work lands on top.

### 2.5 Open issues / tech debt (from regression sweeps)

- **M1:** `pyautogui.position()` called every frame inside the 60 Hz FusionEngine tick — blocking
  syscall in the hot loop. Cache at ≤10 Hz.
- **L2:** autonomous `GIT_COMMIT`/`GITHUB_PR` verbs bypass the `approval_hook` gate.
- `requirements.txt` pins `websockets==14.2` but runtime is 16.x (`.state` vs `.closed` already bit
  KiroClient once).
- Plaintext API key in `docs/API Key Azure Foundry.txt` (gitignored, but rotate + move to Credential
  Manager).

### 2.6 Positioning

- **Research:** "Personal, Local-First Agent OS" framing; target venue **AgenticOS workshop @ SOSP
  2026**. Near-term need: *convert projected CLICK-success figures (42→78→88→92 %) into logged
  within-subject results* — i.e. **real usage data**.
- **Commercial:** 7-phase plan to 100 subscribers / $1K MRR by Sep '27; currently in **Phase 1 —
  Hardening** (goal: an 8-hour session with no restart).

---

## Part 3 — The Central Constraint

Three months of design assume an iPad Pro. The hardware is a standard iPad. **Gaze + head + depth
are dormant.** June is the month that resolves the depth half of this — but note an asymmetry the
RealSense does *not* fix:

- ✅ **RealSense L515 unblocks:** scene depth, gesture-depth validation, the flick/grab physics, and
  (via its RGB) a PC-side hand-tracking feed independent of iPad camera streaming.
- ❌ **RealSense does NOT unblock gaze.** A depth camera is not an eye tracker. (Decision taken
  2026-05-30: **gaze + head-pose were removed entirely** — see Part 4. They are no longer dormant
  code; they are deleted.)

---

## Part 4 — June 2026 Roadmap

**Decisions locked (2026-05-30):** ① **Emphasis = sensor unblock first.** ② **Gaze + head-pose =
removed entirely** (done 2026-05-30 — the standard iPad has no TrueDepth sensor; see CLAUDE.md
status entry). ③ **Scope = engineering only** (no paper writing; capture data now, write later).

**Theme: "Make the depth pipeline real on the RealSense, then prove it holds for 8 hours."** The
RealSense L515 is the spine of the month — get gesture/depth running on real hardware, then harden
for the soak milestone. Gaze/head-pose were removed entirely (2026-05-30); cursor pointing leans on
the modalities that work (tilt, trackpad, touch). Data logging is automatic background capture, not a
writing project.

### Workstream A — Sensor reality (P0, the month's spine) 🎯
- **A1. `sensors/realsense_receiver.py`** — bridge `pyrealsense2` → existing `LiDARReceiver`
  interface; expose `get_depth_at()` + RGB frames; align depth↔color. Add `--realsense` flag to
  `main.py`, wire into `LiDARReceiver` + `GestureProcessor`, degrade gracefully if SDK/camera absent.
  `requirements.txt`: `pyrealsense2`. *First task the day the camera arrives.*
- **A2. Re-point GestureProcessor** at RealSense RGB+depth so the 13-gesture vocabulary
  (peace-swipe / two-finger-grab / snap / monitor / push-pull / pinch) and flick/grab window physics
  run on **real depth for the first time**. Validate live in `sensor_viewer.py` (camera + depth +
  hand-landmark overlay already exist).
- **A3. Calibrate gestures on real data** — with real depth flowing, `ContinuousTrainer` can finally
  learn true velocity floors (p10 observed) and `compute_peace_jitter()` produces a real inflammation
  signal. Tune the 800 ms debounce / axis-dominance thresholds against actual hand motion.
- **A4. Mounting + ergonomics** — physical placement of the L515 for the 0.3–1 m gesture range at the
  fixed desk; document it (mirrors the fixed-chair calibration logic).

### Workstream B — Hardening & soak (P1, commercial Phase 1 milestone)
- **B1. Fix M1** (cache `pyautogui.position()` off the 60 Hz loop) — do this *before* soaking.
- **B2. 8-hour soak test** — full pipeline (now incl. RealSense) under synthetic+real load; watch
  VRAM, latency EMA, memory growth, the (now-functional) 50 Hz watchdog. Capture failures → fix list.
- **B3. Memory-leak / handle audit** under soak — Whisper, ChromaDB WAL, vLLM teardown, websockets,
  **and the new RealSense USB stream** (pyrealsense2 pipelines must be released cleanly).
- **B4. Add the approval gate to destructive git verbs** (L2).

### Workstream C — Land & stabilize inference (P1)
- **C1. Land the uncommitted work cleanly** — split `CloudDevAgent` (mine) from the
  `local_inference.py` WIP (§2.4) into ≥2 commits. **Do this first; everything rebases on it.**
- **C2. Speculative decoding** (ROADMAP #9) — `--speculative-model` now that the vLLM baseline is
  verified; measure tok/s + acceptance rate on code. Quick win.
- **C3. Smoke-validate `CloudDevAgent`** with a live key — confirm `--cloud-dev-agent` fallback and
  `--no-local-specialists` GPU-free path work end-to-end and log real per-query cost. Lightweight
  this month (it's a Phase-3 down-payment, not a June goal).
- **C4. Fix the `websockets` pin.**

### Workstream D — Data capture (P3, background / automatic)
- **D1. Just use it daily** with the existing instrumentation on — `routing_log`, per-tier CLICK
  success (vision/UIA/verifier), latency, and the new gesture-confidence rows accumulate on their own.
  No analysis or writing this month; the goal is to *cross the data thresholds* (routing classifier
  needs 200+ vs. 11 today; gesture model needs real depth, now unblocked by A1–A3) so the AI layers
  and the eventual paper have a corpus when you return to them.

### Sequencing (4 weeks)

| Week | Focus | Exit criteria |
|------|-------|---------------|
| **Jun 1–7** | **A1 RealSense receiver** (camera arrives) · C1 commit hygiene · B1 (M1 fix) | clean git tree; RealSense depth + RGB streaming into `sensor_viewer` |
| **Jun 8–14** | **A2 gestures on real depth** · C2 speculative decoding | a real gesture fires the flick/grab pipeline; spec-decode tok/s measured |
| **Jun 15–21** | **A3 gesture calibration** on real data · A4 mounting · B2 soak begins | velocity floors learned from real motion; first multi-hour run |
| **Jun 22–30** | Fix soak failures → **8-hour clean run** · B3/B4/C3/C4 cleanup · D1 data accrues | 8-hour no-restart milestone; gesture-confidence dataset seeded |

### Removed / deferred by decision
- **Gaze & head-pose — REMOVED entirely (2026-05-30).** Not deferred: the gaze/head pipelines, the
  gaze-monitor-calibration effort (G1–G5, `MonitorCalibrationSheet.swift`), edge-scroll, and the
  TrueDepth Swift sensors are deleted from PC + iPad. If eye control is ever revisited it will be a
  fresh build on a dedicated eye tracker, not a revival of this code.
- **AgenticOS @ SOSP paper** — deferred: engineering only this month; D1 capture feeds it later.

### Explicitly NOT in June (park these)
Multi-user (Phase 5), StoreKit/companion app (Phase 4/6), Mac port beyond the cloud-dev down-payment,
Nemotron RAM offload, sandboxed `RUN_TERMINAL`.
