# Implementation Plan: Integration Tests & Cloud Fallback

## Overview

All cloud fallback components (Bedrock, Transcribe, Polly, cloud latency logging) are already
implemented in `hybrid_coordinator.py` and `command_executor.py`. The standalone integration
test scripts (test_gaze_dwell_e2e.py, test_voice_e2e.py, test_cloud_path.py, test_polly_tts.py,
test_model_router_vram.py, test_dwell_activation.py) are also complete and verified passing.

Sprint 4 also resolved pre-existing infrastructure gaps:
  - Installed `pytest-asyncio` and set `asyncio_mode = auto` (pytest.ini)
  - Installed `boto3==1.38.28` (was in requirements.txt but missing from venv)
  - Added root-level `conftest.py` to exclude standalone integration scripts from pytest collection
  - Fixed 7 regressions from sensor-refinement sprint (pinch_mm rename, _tilt_pos_ema_x removal,
    _arrival_ts → _recv_mono rename, EMA variance property bug, gaze cursor dead-zone assumptions)

## Task Status

### ✅ Wave 1 — Infrastructure

- [x] 1.1 Install pytest-asyncio and configure asyncio_mode = auto in pytest.ini
- [x] 1.2 Install boto3==1.38.28 (listed in requirements.txt, was absent from venv)
- [x] 1.3 Add root-level conftest.py with collect_ignore_glob for standalone WebSocket scripts

### ✅ Wave 2 — Pre-existing regression fixes

- [x] 2.1 Fix gesture_processor tests: pinch_mm → pinch_z_delta_mm (sensor-refinement rename)
- [x] 2.2 Fix gesture_lidar_integration tests: same pinch_mm rename
- [x] 2.3 Fix lidar_receiver test: _arrival_ts → _recv_mono (attribute rename from Phase 6 bugfix)
- [x] 2.4 Fix test_prop_ema_smoothing: replace tests of removed _tilt_pos_ema_x/_y with
      1-Euro-filter-appropriate property tests (bounded screen coords, recurrence on helper)
- [x] 2.5 Fix test_prop_ema_smoothing: correct false EMA variance reduction property
      (EMA variance reduction doesn't hold in general; replaced with recurrence-relation test)
- [x] 2.6 Fix test_prop_gaze_cursor P14: raise dead-zone threshold in assume() from 0.02 → 0.15
      (dead_zone_inner=0.05, outer=0.125; 0.0625 is inside ramp and rounds to 0px movement)
- [x] 2.7 Fix test_prop_gaze_cursor P15: strengthen assume() to require >0.01 delta
      (floating-point values like 0.9999999999999999 ≡ 1.0 in double precision)

### ✅ Wave 3 — Core integration tests (standalone scripts)

All implemented as standalone async scripts runnable via `python tests/<file>.py`:

- [x] 3.1 test_gaze_dwell_e2e.py — FusionEngine Rule 3 → Coordinator bypass → Executor CLICK
      (Properties 1, 2 from design.md; Req 1.1, 1.2, 1.3, 1.5)
- [x] 3.2 test_dwell_activation.py — Gaze stability buffer → dwell timer → CLICK after timeout
      (Property 3; Req 2.1, 2.2, 2.3, 2.4)
- [x] 3.3 test_voice_e2e.py — WhisperStream → FusionEngine Rule 10 → Coordinator → Executor
      (Properties 4, 5, 6; Req 3.1–3.7)
- [x] 3.4 test_model_router_vram.py — ModelRouter.select_profile() VRAM fallback chain
      (Property 7; Req 4.1–4.7)

### ✅ Wave 4 — Cloud fallback tests (standalone scripts)

- [x] 4.1 test_cloud_path.py — Bedrock cloud inference, Gate 2 routing, voice misrecognition,
      graceful degradation (bad credentials → CLARIFY), Gate 0 privacy, AgentCore fall-through
      (Properties 8, 12; Req 5.1–5.7, 6.1–6.7, 9.5)
- [x] 4.2 test_polly_tts.py — _polly_speak() AWS call, CLARIFY cloud route → spoken=True,
      empty message, truncation at 3000 chars, bad credentials → False, sounddevice missing → False
      (Properties 10, 11; Req 7.1–7.7, 9.3)

### ✅ Wave 5 — Cloud latency logging

Cloud latency logging is exercised within test_cloud_path.py:
- Gate 2/3/4 failure routes insert commands table rows with route="cloud"
- gate_that_decided field carries the correct gate label
- latency_ms > 0 for all cloud routes
- ContinuousTrainer reads these rows via get_recent_routing_stats()

(Property 9; Req 8.1–8.6)

### ✅ Wave 6 — Graceful degradation validation

Covered across existing test files:
- Bad credentials → CLARIFY (no crash): test_cloud_path.py test 4
- Transcribe timeout → returns original cmd: test_voice_e2e.py retranscription test
- Polly failure → spoken=False: test_polly_tts.py tests 6, 7
- VRAM check failure → 999 GB assumed: test_model_router_vram.py tests 4, 5
- Gate 0 prevents cloud call on sensitive text: test_cloud_path.py test 5

(Property 12; Req 9.1–9.6)

## Notes

- All standalone integration scripts follow the established pattern from test_touch_scroll_e2e.py:
  asyncio.run() entry point, mocked I/O, ✓/✗ result lines, sys.exit(0/1)
- Standalone scripts are excluded from pytest collection via root-level conftest.py
  (they use non-pytest patterns: function parameters as mock objects, not fixtures)
- Tests that make real AWS calls (test_cloud_path.py test 1, test_polly_tts.py test 1)
  require valid AWS credentials in ~/.aws/credentials with Bedrock and Polly access
- boto3 v1.38.28 is pinned in requirements.txt and must be installed in the venv

## Test Coverage Matrix

| Test File | Pipeline Path | Mocked | AWS Required |
|-----------|--------------|--------|--------------|
| test_gaze_dwell_e2e.py | FusionEngine Rule 3 → bypass → CLICK | OllamaInference, mouse | No |
| test_dwell_activation.py | Gaze stability → dwell timer → CLICK | pyautogui, time | No |
| test_voice_e2e.py | WhisperStream → Rule 10 → gates → action | faster_whisper, sounddevice, OllamaInference | No |
| test_model_router_vram.py | ModelRouter VRAM fallback chain | pynvml | No |
| test_cloud_path.py | Gate 2 fail → Bedrock | boto3 (most tests) | Test 1, 3 only |
| test_polly_tts.py | CLARIFY cloud → Polly TTS | boto3 (most tests) | Test 1 only |
