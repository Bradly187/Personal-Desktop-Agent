# Tasks

## Phase 1 — Core pipeline (coordinator + command execution + MCP server)

- [x] 1.1 Set up project structure and install core dependencies
- [x] 1.2 Implement `hybrid_coordinator.py` (4-gate routing, VRAM monitor, outcome logger)
- [x] 1.3 Implement `command_executor.py` (11 accessibility verbs + 5 dev verbs)
- [x] 1.4 Implement `local_inference.py` (OllamaInference, NemotronInference, VLLMInference stub)
- [x] 1.5 Implement MCP server (`mcp_server/desktop_mcp_server.py`, 14 tools, stdio transport)
- [x] 1.6 Implement `main.py` (unified entry point, --measure-vram, --safe-mode, startup table, graceful shutdown)
- [x] 1.7 Integration test: touch_command "scroll down" end-to-end (iPad → Bridge → Executor → pyautogui)

---

## Phase 2 — iPad sensor integration

- [x] 2.1 Implement `ipad_bridge.py` (WebSocket :8765, mDNS, 13 message types routed)
- [x] 2.2 Implement `fusion_engine.py` (60 Hz tick, 10 priority rules)
- [x] 2.3 Implement iPad Swift app (TiltSensor, GazeTracker, HeadTracker, KeywordListener, SoundDetector)
- [x] 2.4 Implement iPad UI (CommandPadView, TrackpadView, ScientificKeypadView, HandwritingCanvasView, SettingsView)
- [x] 2.5 Implement WebSocketManager with exponential backoff and Bonjour/mDNS discovery
- [x] 2.6 Implement `gesture_processor.py` (MediaPipe Hands, POINT/PINCH/OPEN_PALM/FIST, LiDAR depth, debounce)
- [x] 2.7 Implement `lidar_receiver.py` (depth_frame decode, confidence filtering, bilinear depth query)
- [x] 2.8 Implement web_client/ Safari fallback UI
- [x] 2.9 Integration test: tilt navigation moves cursor (FusionEngine rule 6 + Core Motion)
- [ ] 2.10 Integration test: gaze dwell fires click (FusionEngine rule 3 + GazeTracker)
- [x] 2.11 Integration test: iPad trackpad drag moves cursor proportionally
- [ ] 2.12 Integration test: dwell activation fires after configured timeout without tap
- [x] 2.13 Implement full `VLLMInference`; benchmark vs OllamaInference on RTX 5090

---

## Phase 3 — Voice pipeline (WhisperStream)

- [x] 3.1 Install `faster-whisper` (pinned in requirements.txt)
- [x] 3.2 Install `sounddevice` and pin in requirements.txt
- [x] 3.3 Implement `whisper_stream.py`
      - `SileroVAD` loading from `torch.hub` with 512-sample chunk processing
      - `AudioBuffer` async ring buffer
      - `UtteranceSegmenter` state machine (IDLE → CAPTURING → emit)
      - `WhisperTranscriber` with `faster-whisper`, `float16`, `large-v3`
      - `MicCapture` bridging `sounddevice` callback to async queue
      - `WhisperStream` with concurrent capture + dispatch tasks
- [x] 3.4 Wire WhisperStream into main.py pipeline assembly
- [ ] 3.5 Integration test: voice command "open chrome" executes end-to-end (< 600 ms)

---

## Phase 4 — Continuous training

- [x] 4.1 Implement `continuous_trainer.py` skeleton (few-shot SQLite, threshold adaptation, hotword promotion, gesture calibration)
- [ ] 4.2 1-week routing log soak — accumulate `routing_log.jsonl` entries
- [ ] 4.3 Validate ContinuousTrainer gate1 threshold adaptation from soak data
- [ ] 4.4 Validate hotword promotion writes correct entries to `hotwords.txt`
- [ ] 4.5 Verify that after 50 interactions, `few_shot_memory.db` contains correct entries
      and augmented prompts include relevant examples

---

## Phase 5 — Domain routing + specialist models

- [x] 5.1 Implement `domain_classifier.py` (keyword-scoring, 6 domains)
- [x] 5.2 Implement `model_router.py` (VRAM-aware specialist selection, fallback chains)
- [x] 5.3 Implement `dev_agent.py` (plan→execute→reflect loop, 5 dev verbs)
- [x] 5.4 Pull specialist models (`ollama pull qwen3-coder:30b`, `deepseek-r1:8b`, `qwen3-vl:30b`)
- [x] 5.5 Integration test: DevAgent handles code/math/vision/plan queries correctly
- [ ] 5.6 Integration test: ModelRouter falls back when VRAM insufficient for specialist

---

## Phase 6 — AWS cloud fallback

- [ ] 6.1 Configure AWS credentials and region
- [ ] 6.2 Test `CloudInference` routes to Bedrock when Gate 2 fires (multi-step command)
- [ ] 6.3 Test `CloudInference` routes to Transcribe when Gate 1 fires (low Whisper confidence)
- [ ] 6.4 Add Amazon Polly TTS fallback in `CommandExecutor._clarify()` for cloud path
- [ ] 6.5 Verify cloud latency is logged in `routing_log.jsonl` and feeds threshold tuner

---

## Phase 7 — Hardening and polish

- [x] 7.1 Write `requirements.txt` with pinned versions
- [x] 7.2 Add startup status table (main.py --measure-vram)
- [x] 7.3 Graceful Ctrl-C shutdown (save calibration, flush logs, stop streams)
- [x] 7.4 Pin remaining dependencies (`mediapipe`, `ultralytics`, `sounddevice`, `aiosqlite`, `duckdb`, `sentence-transformers`) once installed
- [ ] 7.5 Write `README.md` with hardware setup checklist, dependency install, quick-start
- [ ] 7.6 Add `--calibrate` flag for 9-point gaze calibration routine
- [ ] 7.7 Run `benchmark_models.py` on RTX 5090 and commit results
