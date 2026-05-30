# Daily Review — 2026-05-30

## Yesterday's Work (2026-05-29)

### 1. vLLM Baseline Verified ✅

Committed updates to `ROADMAP.md` and `CLAUDE.md` reflecting that vLLM 0.21.0 +
torch 2.11.0+cu128 is now confirmed working in Ubuntu WSL2 on the RTX 5090
(sm_120 Blackwell arch). Key production notes recorded:

- Server must be started from within WSL (automount disabled in `wsl.conf`)
- `--gpu-memory-utilization 0.65` required when Whisper is co-loaded (~4.2 GB)
- `0.75` standalone
- Ninja JIT build tool must be on PATH (`~/.local/bin/ninja`)
- Activate with `--backend vllm` in `main.py`
- Speculative decoding (roadmap #9) is now unblocked

VisionGrounder also switched from `claude-sonnet-4-6` to local `qwen3-vl:30b`
via Ollama as the primary backend (Anthropic API as automatic fallback), saving
~$10/month. Chatterbox TTS activated in `approval_config.json` (`tts_backend:
"chatterbox"`), saving ~$6/month in Polly charges. Total estimated savings:
~$35–54/month from $80 baseline.

### 2. Package Restructuring (staged, not yet committed)

All root-level Python modules reorganized into domain packages. Old flat layout
replaced with:

| Package | Modules moved in |
|---------|-----------------|
| `core/` | `ipad_bridge`, `command_executor`, `fusion_engine`, `hybrid_coordinator`, `domain_classifier` |
| `adaptive/` | `behavioral_twin_state`, `continuous_trainer`, `content_filter`, `mcp_trust_classifier` |
| `sensors/` | `gesture_processor`, `whisper_stream`, `lidar_receiver`, `one_euro_filter`, `sensor_viewer` |
| `storage/` | `db`, `audit_log`, `semantic_memory`, `session_analyzer` |
| `inference/` | `local_inference`, `model_router`, `dev_agent`, `kiro_client`, `codebase_indexer` |
| `calibration/` | `acoustic_profiler`, `voice_calibrator`, `gaze_calibrator`, `calibration_overlay`, `gyro_bias_calibrator` |
| `desktop/` | `vision_grounder`, `ui_automation`, `action_verifier`, `flick_engine`, `snap_zones` |
| `monitoring/` | `metrics`, `dashboard`, `benchmark_models` |
| `tts/` | `polly_stream`, `chatterbox_tts` |

Each package has an `__init__.py`. All cross-package imports updated throughout
(e.g. `from core.fusion_engine import FusionEngine`). `main.py` updated to use
new package paths. All test files updated. Docs reorganized:

- `docs/daily/` — daily review files (was `docs/`)
- `docs/architecture/` — architecture and handoff docs
- `docs/diagrams/` — PNG/SVG diagram files
- `docs/research/` — research PDFs

---

## Housekeeping (2026-05-30)

### Stale Reference Fixes

**`CLAUDE.md` — Key Files table** updated to reflect new package paths for all
28 module entries. Added 5 missing entries: `desktop/flick_engine.py`,
`inference/kiro_client.py`, `inference/codebase_indexer.py`,
`monitoring/metrics.py`, `storage/session_analyzer.py`.
Daily reviews path updated: `docs/` → `docs/daily/`.
TTS paths table and Sensor Priority section updated to use `core/`, `tts/`
prefixes.

**`.kiro/steering/tech.md`** — vLLM status updated from "blocked on CUDA build"
to "verified in WSL2". Build commands updated: `python ipad_bridge.py` →
`python -m core.ipad_bridge`; `python benchmark_models.py` →
`python monitoring/benchmark_models.py`. `VLLMInference` inline comment updated.
`LocalInference` pattern description updated.

### Bug Fixes (from 2026-05-27 regression report)

Three open bugs from `RegressionTest.md` were confirmed still present and fixed:

**H1 — KiroClient socket leak (`inference/kiro_client.py`)**
`getattr(self._ws, "closed", True)` always returned `True` on websockets ≥14
because `ClientConnection` exposes `.state` not `.closed`. Result: a new
WebSocket was opened on every request, leaking sockets.

Fix: introduced `_is_closed()` helper that checks `ws.state is not State.OPEN`
via `websockets.connection.State`, falling back to the `getattr` pattern for
older versions. Applied to both `_do_request` and `get_status`.

**H2 — Metrics gauges `whisper_logprob` / `gesture_conf` always NULL
(`core/hybrid_coordinator.py`)**
`record_command_outcome` was reading `cmd.params.get("whisper_logprob")` and
`cmd.params.get("gesture_conf")` — fields that are never stored in the params
dict. The correct values are `cmd.whisper_logprob` and
`cmd.gesture_confidence` (direct `Command` dataclass attributes).

Fix: replaced both `cmd.params.get(...)` calls with
`getattr(cmd, "whisper_logprob", None)` and `getattr(cmd, "gesture_confidence", None)`.

**M2 — `rms_ambient` telemetry never written (`main.py`)**
`FusionEngine.set_acoustic_profiler()` exists and the telemetry row references
`self._acoustic_profiler`, but `main.py` never called the wiring method.
`whisper` and `twin_state` both got wired, but `fusion` did not.

Fix: added `fusion.set_acoustic_profiler(profiler)` in `_run_pipeline` after the
profiler is loaded and before gaze calibrator setup.

### Compile Check

All 9 new packages (core, adaptive, sensors, storage, inference, calibration,
desktop, monitoring, tts) and `main.py` pass `py_compile` with no errors.

---

## Open Items

- **Restructuring not yet committed** — the staged package reorganization
  represents a large diff. Should be committed as a single refactor commit with
  a clear message before the next feature work.
- **M1 (medium)** — `pyautogui.position()` called inside the 60 Hz tick loop
  (`core/fusion_engine.py`) is a blocking call on every frame. Should be moved
  to a cached value updated at 10 Hz or less.
- **L2 (low)** — Autonomous git/GitHub verbs in `inference/dev_agent.py` bypass
  the `approval_hook.py` gate. Low risk now (only fires when user explicitly uses
  dev verbs), but worth adding a confirmation step before destructive git ops.
- **websockets version pin** — `requirements.txt` still pins `websockets==14.2`
  but the installed runtime is 16.0. Should update the pin or add a compat shim.
