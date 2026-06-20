# Desktop Agent — Architecture, DB Schema & Behaviour Rundown

Generated: 2026-05-25

> **⚠️ Partially superseded (snapshot of 2026-05-25).** Since this was written: eye-gaze and
> head-pose control were **removed** (2026-05-30 — the standard iPad lacks TrueDepth); the laptop
> compute cluster was **excised** (the agent is single-machine local-only); the Kiro IDE bridge was
> renamed to the **VS Code bridge** (`inference/bridge_client.py`, `desktop-agent-bridge/`, `--vscode`);
> `agent.db` is now **42 tables at `PRAGMA user_version = 8`**; the FusionEngine priority is **6-level**;
> iPad→PC has **25** message types. `CLAUDE.md` + `storage/db.py` are the authoritative current sources.

---

## 1. Status

**Phases 1–6 + Sprints A–C + 5–7 + G1–G4 complete.**  All core pipeline stages are built and wired end-to-end. The Swift iPad app is a separate subsystem; this document covers the Python PC side only.

| Area | State |
|------|-------|
| WebSocket bridge (ipad_bridge.py) | ✅ Prod-ready — 28 message types |
| Sensor fusion (fusion_engine.py) | ✅ 60 Hz, 10-level priority, OneEuroFilter |
| Gate routing (hybrid_coordinator.py) | ✅ 5 gates, Bedrock fallback |
| Local inference (local_inference.py) | ✅ Ollama active; vLLM code complete, needs CUDA 13.x |
| Dev agent (dev_agent.py) | ✅ Plan→execute→reflect, 5 dev verbs |
| Vision grounding (vision_grounder.py) | ✅ claude-sonnet-4-6, ≥0.7 confidence |
| UIAutomation (ui_automation.py) | ✅ Win32 BFS, fuzzy scoring |
| Action verification (action_verifier.py) | ✅ Pillow pre/post diff |
| Voice pipeline (whisper_stream.py) | ✅ Silero VAD + faster-whisper large-v3, GPU |
| Gesture processor (gesture_processor.py) | ✅ MediaPipe, 13 gestures |
| ~~Gaze calibration (gaze_calibrator.py)~~ | ❌ Removed 2026-05-30 (no TrueDepth on the standard iPad) |
| AgentDB (db.py) | ✅ 42 tables at v8, WAL mode, MiniLM retrieval |
| Continuous trainer (continuous_trainer.py) | ✅ Threshold adaptation, pain-day −30% |
| Acoustic profiler (acoustic_profiler.py) | ✅ VAD/logprob calibration, drift detection |
| TTS (polly_stream.py / chatterbox_tts.py) | ✅ Danielle neural or local GPU |
| Approval hook (approval_hook.py) | ✅ PreToolUse voice gate |
| MCP server (desktop_mcp_server.py) | ✅ 14 tools, stdio |

**One known gap:** the voice-triggered gaze monitor calibration command (`"hey agent calibrate monitor"` → overlay → solve → TTS report) and `MonitorCalibrationSheet.swift` are listed in CLAUDE.md as remaining work from Sprint G4.

---

## 2. System Architecture

The desktop agent is a 7-layer async Python pipeline. All inter-layer communication uses the `Command` dataclass as a universal DTO.

```
┌─────────────────────────────────────────────────────────────────────┐
│  INPUT LAYER                                                        │
│  ipad_bridge.py   whisper_stream.py   gesture_processor.py         │
│  aiohttp WS:8765  Silero VAD+Whisper  MediaPipe HandLandmarker      │
│  28 msg types     large-v3 GPU        13 gestures · 500ms buffer    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Command DTO (source, text, params)
┌──────────────────────────▼──────────────────────────────────────────┐
│  FUSION LAYER                                                       │
│  fusion_engine.py                behavioral_twin_state.py           │
│  60 Hz tick                      PainDayEngine                      │
│  10-level sensor priority        PreferenceModel · TwinSnapshot     │
│  OneEuroFilter · GyroBias        ChromaDB backing                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ priority-ranked Command
┌──────────────────────────▼──────────────────────────────────────────┐
│  COORDINATION LAYER                                                 │
│  hybrid_coordinator.py             domain_classifier.py            │
│  Gate 0: Privacy                   COMMAND · CODE · MATH           │
│  Gate 1: Confidence (logprob)      VISION · PLAN · GENERAL         │
│  Gate 2: Complexity (tokens)                                        │
│  Gate 3: VRAM free ≥ 8 GB                                          │
│  Gate 4: Latency EMA                                               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ domain-routed Command
┌──────────────────────────▼──────────────────────────────────────────┐
│  INFERENCE LAYER                                                    │
│  local_inference.py         dev_agent.py        AWS Bedrock         │
│  OllamaInference            plan→execute        claude-haiku-4-5   │
│  llama3.1:8b (active)       →reflect            cloud fallback      │
│  VLLMInference (code ready) 5 dev verbs         gates 2-4 fail     │
│  model_router.py — VRAM-aware specialist selection                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ resolved action + target
┌──────────────────────────▼──────────────────────────────────────────┐
│  RESOLUTION LAYER                                                   │
│  ui_automation.py           vision_grounder.py   gaze_calibrator   │
│  Win32 UIAutomation BFS     claude-sonnet-4-6    5-point affine    │
│  fuzzy name scoring         ≥0.7 confidence      numpy lstsq        │
│  1s cache                   2s cache             JSON + AgentDB     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ pixel coords
┌──────────────────────────▼──────────────────────────────────────────┐
│  EXECUTION LAYER                                                    │
│  command_executor.py        mcp_server/tools/    action_verifier   │
│  16 verbs                   mouse · keyboard     Pillow diff        │
│  11 accessibility           screen · windows     2% threshold       │
│  5 dev                      handwriting          400ms delay        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ pyautogui / win32gui / win32clipboard
                    Windows Desktop
```

**Rendered diagram files:**
> ⚠️ **Stale — pending regeneration.** `architecture-desktop-agent.svg` still depicts removed
> gaze/head-pose nodes and the old 10-level priority. The current mermaid **sources** under
> `docs/diagrams/overview/*.mmd` and `docs/diagrams/state/*.mmd` are accurate — regenerate the SVG/PNG from those (or drop these exports).
- [`architecture-desktop-agent.svg`](architecture-desktop-agent.svg)
- [`architecture-desktop-agent.png`](architecture-desktop-agent.png)

---

## 3. DB Schema

AgentDB is a single SQLite file (`agent.db`) with WAL mode, 42 tables (`PRAGMA user_version = 8`), and MiniLM semantic retrieval for few-shot examples.
AnalyticsDB is a DuckDB sidecar (`analytics.duckdb`) that can attach `agent.db` directly for analytical queries.

### 3.1 Table Inventory

| Group | Tables |
|-------|--------|
| **Session / Command** | `sessions`, `commands`, `inferences`, `agent_runs`, `agent_steps` |
| **Learning** | `few_shot_examples`, `word_counts`, `hotwords`, `settings_versions` |
| **Gesture** | `gesture_samples`, `gesture_calibration`, `gesture_velocity_samples`, `gesture_velocity_calibration` |
| **Sensor / Twin** | `sensor_events`, `twin_session_history`, `twin_pain_day_log`, `adaptation_log` |
| **Voice** | `voice_calibration`, `voice_profile`, `voice_phrases`, `voice_calibration_sessions`, `voice_pronunciations`, `voice_profiles`, `ambient_transcripts` |
| **Accessibility** | `sensor_rom`, `flare_profile` |
| **Gaze / iPad** | `gaze_monitor_calibration`, `ipad_logs` |

### 3.2 Core Schema — Key Tables

```sql
-- Anchor for everything else; one row per process run
sessions (id, started_at, ended_at, mode, git_hash, agent_version, notes)

-- Every command that enters the pipeline
commands (id, session_id→sessions, ts, source, text, action, params,
          route, gate_that_decided, latency_ms, whisper_logprob,
          gesture_confidence, gaze_x, gaze_y, success, error_msg, corrected_to)

-- LLM call record per command
inferences (id, command_id→commands, ts, model, domain, prompt_hash,
            response, tokens_in, tokens_out, latency_ms, backend, error)

-- DevAgent plan/execute/reflect runs
agent_runs (id, command_id→commands, ts, goal, domain, model_used,
            step_count, success, total_latency_ms, error)
agent_steps (id, run_id→agent_runs, step_num, action, args, body,
             result, success, latency_ms)

-- MiniLM-embedded few-shot examples (384-dim BLOB)
few_shot_examples (id, command_id→commands, text, action, source, domain,
                   ts, usage_count, embedding BLOB)

-- Gesture confidence and velocity learning
gesture_samples            (id, command_id, ts, gesture, confidence, lidar_depth_m)
gesture_calibration        (id, ts, gesture, confidence_floor, sample_count, p10)
gesture_velocity_samples   (id, ts, gesture, velocity, pain_day)
gesture_velocity_calibration (id, ts, gesture, velocity_floor, sample_count, p10)

-- Sensor events and settings audit
sensor_events     (id, command_id, ts, event_type, x, y, confidence, value, params)
settings_versions (id, ts, component, key, old_value, new_value, changed_by)
adaptation_log    (id, ts, component, metric_before, metric_after,
                   cloud_rate, failure_rate, rolled_back)

-- BehavioralTwinState history
twin_session_history (id, session_id, ts, cmd_text, action, source, seq)
twin_pain_day_log    (id, session_id, ts, pain_day_score, pain_day_active,
                      fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta)

-- Voice calibration (Sprint A/C)
voice_calibration      (id, session_id, ts, phrase, actual_text, rms_amplitude,
                        freq_centroid, avg_logprob, duration_s, is_flare_day)
voice_profile          (id, updated_at, baseline_rms, baseline_logprob, baseline_freq,
                        flare_rms_scale, vad_threshold, logprob_floor, sample_count)
voice_calibration_sessions (id, ts, condition, notes)
voice_pronunciations   (id, session_id→voice_calibration_sessions, ts,
                        expected, heard, logprob, duration_s, accepted)
voice_profiles         (id, condition UNIQUE, corrections_json, vad_threshold,
                        logprob_floor, initial_prompt, updated_at)

-- Accessibility / ROM
sensor_rom    (id, ts, session_id, sensor, direction, max_value, comfortable_value, unit)
flare_profile (id, updated_at, voice_degrades, gesture_degrades, gaze_degrades,
               tilt_degrades, flare_vad_scale, manual_pain_day, notes)

-- Lecture mode
ambient_transcripts (id, session_id, ts, text, logprob, duration_s)

-- iPad structured log forwarding (warning+ persisted)
ipad_logs (id, session_id, ts, level, subsystem, msg)

-- Gaze-to-monitor angular affine mapping
gaze_monitor_calibration (id, session_id, ref_dir JSON, matrix JSON [2×3],
                           screen_w, screen_h, sample_count, residual_px, created_at)
```

**Rendered schema files:**
> ⚠️ **`db-schema-calibration.svg` is stale — pending regeneration** (still shows the removed
> `gaze_monitor_calibration` table). Regenerate from the current mermaid sources under
> `docs/diagrams/db/*.mmd`. (`db-schema-pipeline.svg` is current.)
- Core pipeline: [`db-schema-pipeline.svg`](db-schema-pipeline.svg) / [`db-schema-pipeline.png`](db-schema-pipeline.png)
- Calibration & voice: [`db-schema-calibration.svg`](db-schema-calibration.svg) / [`db-schema-calibration.png`](db-schema-calibration.png)

---

## 4. Behaviour Rundown

### 4.1 Normal command flow (e.g. "click Firefox")

1. iPad mic → `whisper_stream.py`: Silero VAD fires, faster-whisper transcribes at ~373ms warm. Hallucination filter (`no_speech_prob > 0.5` || `avg_logprob < -0.8`) drops noise. Wake phrase check (`"hey agent"`). Emits `Command(source="voice", text="click Firefox", whisper_logprob=-0.3)`.

2. `fusion_engine.py` tick: Command arrives at priority 10 (voice). If tilt/head/gaze events also pending, they are processed at higher priority first. The voice command is queued and dispatched next tick.

3. `hybrid_coordinator.py` gates:
   - Gate 0: "click Firefox" contains no sensitive patterns → pass
   - Gate 1: `whisper_logprob=-0.3 ≥ -1.0` → pass
   - Gate 2: 2 tokens ≤ 40 → pass
   - Gate 3: VRAM free ≥ 8 GB → pass (RTX 5090 has ~19 GB free)
   - Gate 4: latency EMA ≤ budget → pass
   - → routes to `OllamaInference(llama3.1:8b)` via `domain_classifier` (COMMAND domain)

4. `local_inference.py`: builds verb-first prompt, calls Ollama HTTP API, gets `CLICK Firefox`. Parses to `action=CLICK, params={target:"Firefox"}`.

5. `command_executor.py._resolve_coords`:
   - Try `UIAutomationProvider` → BFS Win32 tree for "Firefox" → found at (960, 540)
   - If not: try `VisionGrounder` (claude-sonnet-4-6 screenshot) → fallback
   - If not: gaze coords → if not: screen centre + CLARIFY

6. `action_verifier.py`: takes pre-screenshot. `command_executor` calls `mouse.click(960, 540)`. `action_verifier` takes post-screenshot 400ms later, computes Pillow diff → 3.2% pixel change → success.

7. Outcome logged to `commands` table; `few_shot_examples` updated (embedding via MiniLM); `continuous_trainer` drains velocity queue.

### 4.2 Pain-day behaviour

`BehavioralTwinState.PainDayEngine` scores 5 signals: voice clarity (AcousticProfiler), gesture confidence drop, command failure ratio, CLARIFY ratio, command rate. When score > threshold or `manual_pain_day=1` (sent from iPad FlareProfileSheet in < 100ms):

- `fusion_engine.apply_pain_day()`: relaxes 6 FusionConfig thresholds (tilt deadzone wider, gaze stability lower, etc.)
- `GestureProcessor`: velocity floor *= 0.70
- `WhisperStream`: VAD threshold relaxed per `voice_profile.flare_rms_scale`
- `HybridCoordinator`: logs to `twin_pain_day_log`

### 4.3 Tilt cursor (bypass LLM)

Tilt events from iPad Core Motion arrive as `tilt` or `tilt_position` WebSocket messages. `FusionEngine` processes them at priority 6 — they call `pyautogui.moveRel()` directly, no Command DTO, no LLM, no gate evaluation. The gyro bias calibrator subtracts learned drift before the OneEuroFilter smooths the signal.

### 4.4 Gesture commands

Peace-sign base pose (index + middle extended). `gesture_processor.py` runs a 500ms rolling frame buffer. Dominant-axis velocity is compared to a calibrated `velocity_floor` (p10 of recent samples). Debounce is 800ms. On valid gesture, a `Command(source="gesture")` is emitted to `FusionEngine` at priority 8 → full coordinator (including Gate 1 confidence check → discard if below floor).

### 4.5 Gaze calibration

`gaze_calibrator.py` uses a 5-point angular affine mapping from ARKit world-space gaze rays to screen pixels. At startup, `main.py` loads the calibration from `gaze_calibration.json` and `gaze_monitor_calibration` table. On each `gaze_dwell` event, if a fresh gaze ray (< 300ms old) is attached, `GazeCalibrator.project(ray_dir)` overrides the (x, y) coordinates with calibrated absolute pixel position instead of cumulative delta.

### 4.6 DevAgent flow

For CODE/MATH/VISION/PLAN/GENERAL domains, `model_router.py` selects a VRAM-appropriate specialist and `dev_agent.py` runs a plan→execute→reflect loop with 5 verbs: `WRITE_FILE`, `RUN_TERMINAL`, `EXPLAIN`, `SEARCH_WEB`, `READ_SCREEN`. Steps are logged to `agent_runs` + `agent_steps`. If a step fails, the reflect phase updates the plan before the next attempt.

### 4.7 Voice approval hook

When Claude Code (via MCP) wants to run a dangerous tool (Bash, PowerShell, Agent), `approval_hook.py` fires as a `PreToolUse` hook. Danielle TTS speaks the action description. The iPad mic (or PC mic fallback) records the next utterance → WhisperStream → yes/no → exit 0 (approve) or exit 2 (deny). The result is logged to `audit.db` (append-only, no UPDATE/DELETE).

### 4.8 Cloud fallback path

If any of Gates 2–4 fail, `hybrid_coordinator.py` sends the command to `boto3` Bedrock with `us.anthropic.claude-haiku-4-5-20251001-v1:0`. The cloud system prompt includes accessibility misrecognition corrections (e.g. `"tight"→TYPE`). Gate 0 (privacy) always forces local — sensitive commands never reach Bedrock.

---

## 5. Open Work

- Voice-triggered gaze calibration command (`"hey agent calibrate monitor"`) wiring  
- `MonitorCalibrationSheet.swift` iPad UI  
- vLLM activation (needs CUDA 13.x torch wheels for RTX 5090 Blackwell)  
- `ContinuousTrainer` routing-log data is still sparse (11 entries vs. 200+ needed for routing classifier)
