# Key Files

Annotated map of every significant file in the pipeline. Loaded on-demand — not always in context.

| File | Purpose |
|------|---------|
| `core/ipad_bridge.py` | aiohttp WebSocket server on :8765; routes 25 incoming message types; sends `ack`, `status`, `screenshot`, `handwriting_result`, `mic_state`, `recalibration_request` replies |
| `core/command_executor.py` | Maps 16 action verbs to mcp_server tool calls; `_resolve_coords` falls back to screen centre; SCREENSHOT defaults to active window and copies to Windows clipboard |
| `mcp_server/desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| `mcp_server/tools/mouse.py` | move, click, double_click, scroll, drag |
| `mcp_server/tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| `mcp_server/tools/screen.py` | screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR) |
| `mcp_server/tools/windows.py` | get_active_window, list_windows, focus_window (win32gui + psutil) |
| `mcp_server/tools/handwriting.py` | pix2tex LaTeX OCR; latex_to_unicode fallback converter |
| `core/fusion_engine.py` | 60 Hz tick loop; 6-level sensor priority; direct pyautogui for tilt (gaze/head removed) |
| `core/hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); cloud fallback via `core/cloud_backend.py` (Amazon Bedrock when `AWS_BEARER_TOKEN_BEDROCK` set; 10s timeout circuit-breaker); local-inference circuit-breaker (`local_timeout_s`, default 15s → CLARIFY); LLM output schema validation (`_parse_action` + `_VALID_COMMAND_VERBS`; malformed verb → CLARIFY); outcome logger |
| `inference/local_inference.py` | `LocalInference` ABC; `OllamaInference` (default, ~190ms warm wall p50 / ~29ms compute, Ollama 0.30.6), `VLLMInference` (verified in Ubuntu WSL2, vLLM 0.21.0; `--backend vllm`; use `--gpu-memory-utilization 0.65` with Whisper running) |
| `adaptive/continuous_trainer.py` | Routing threshold adaptation; few-shot ranking; gesture velocity-floor calibration (p10 observed, −30% pain day); delegates all storage to `AgentDB`; holds `gesture_processor=` ref for live threshold push-back |
| `sensors/lidar_receiver.py` | Decodes depth_frame messages; confidence-map filtering; `get_depth_at()` |
| `adaptive/behavioral_twin_state.py` | Persistent user behaviour model: `TwinSnapshot`, `PreferenceModel`, `PainDayEngine`; AgentDB + ChromaDB backing; feeds `HybridCoordinator` before every gate decision |
| `storage/semantic_memory.py` | ChromaDB vector store (all-MiniLM-L6-v2, cosine space) for semantic few-shot retrieval; query results carry a `score` (=1−distance); Jaccard fallback when chromadb unavailable; time-gated `_available` re-probe (gated on `_started_once`); `stop()` releases WAL file handles on Windows |
| `sensors/one_euro_filter.py` | Casiez 2012 adaptive low-pass filter (1€); used for tilt velocity, tilt position, gaze delta, head tracking — replaces EMA throughout sensor pipelines |
| `calibration/gyro_bias_calibrator.py` | Gyro bias state machine (UNCALIBRATED→COLLECTING→CALIBRATED→FROZEN); stationary detection + lerp-smoothed bias subtraction for tilt velocity pipeline |
| `sensors/gesture_processor.py` | MediaPipe Tasks API (`HandLandmarker`, `hand_landmarker.task`); peace-sign base pose; 13 gestures (swipe/grab/snap/monitor/push-pull/pinch); 500ms rolling buffer; velocity learning; 800ms debounce |
| `core/domain_classifier.py` | Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL |
| `inference/model_router.py` | VRAM-aware specialist model selection; domain-tuned prompts; Ollama inference |
| `inference/dev_agent.py` | Plan→execute→reflect agentic loop; 5 dev verbs; session context. When the planner declares step deps (`(after: N, M)`), `_run_dag_waves` runs the plan as a dependency DAG — fan-out-safe ready steps (reads/WRITE_FILE/EXPLAIN) run concurrently via `scheduler.fan_out`, barriers (RUN_TERMINAL/git/UI) run solo; first failure/cancel/unmet-dep falls back to the sequential+replan loop. R-10: a `max_replans`/`max_steps` halt rolls back (saga), then `_record_escalation` persists the goal to `dev_escalations` for human review (user cancel never escalates); voice "review queue" / "clear review queue" in the coordinator |
| `main.py` | Unified entry point; `--measure-vram`; `--viewer`/`--viewer-only`; startup status table; Ctrl-C shutdown |
| `sensors/sensor_viewer.py` | tkinter desktop window (daemon thread); camera + LiDAR depth side-by-side; hand landmark overlay; freeze-frame; depth-at-cursor readout; always-on-top toggle |
| `sensors/whisper_stream.py` | GPU-accelerated speech: Silero VAD + faster-whisper large-v3; emits `Command(source="voice")` to FusionEngine |
| `storage/db.py` | `AgentDB` (aiosqlite, all pipeline writes; versioned via `PRAGMA user_version`) + `AnalyticsDB` (DuckDB, 3 benchmark tables); MiniLM semantic retrieval; gesture velocity + voice + iPad log tables; **`goal_queue`** durable goal backlog (gap D — enqueue/claim/complete/requeue_stale, idempotency_key); **`dev_escalations`** human-review backlog (R-10 — insert/get_pending/count/resolve); per-domain inference stats + `adaptation_log.domain` (gap H) |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 8 test messages; verifies ack for each |
| `tts/polly_stream.py` | Python TTS client — HTTP to Node.js sidecar; `speak_sync()` for threads, `speak()` async, `speak_stream()` for token-by-token; auto-starts sidecar; `get_client(backend=)` dispatches to Chatterbox when configured |
| `tts/chatterbox_tts.py` | Local GPU TTS backend (RTX 5090); `ChatterboxClient` with same interface as `PollyStreamClient`; emotion exaggeration, paralinguistic tags, zero-shot voice cloning |
| `tts_service/server.js` | Node.js sidecar (port 8766); calls `StartSpeechSynthesisStream` (AWS SDK v3); returns OGG Vorbis; Python decodes with soundfile |
| `approval_hook.py` | Claude Code `PreToolUse` gate; Danielle speaks action description; records iPad mic via WhisperStream signal file or PC mic fallback; yes/no → exit 0/2 |
| `storage/audit_log.py` | Append-only `audit.db` (SQLite WAL); records every MCP tool invocation, session lifecycle event, and security finding; UPDATE/DELETE blocked by triggers |
| `approval_config.json` | Per-tool approval policy (`"approve"` / `"silent"`), voice, mic device (`"Microphone (Realtek USB Audio)"`), timeout, tts_backend |
| `start_agent.bat` | Windows startup script; activates venv and runs `main.py`; logs to `logs/agent_startup.log` |
| `calibration/acoustic_profiler.py` | Per-user VAD threshold + logprob floor from measured RMS/spectral-centroid/Whisper-logprob; passive calibration; drift detection; seasonal re-cal prompt; Signal 5 in PainDayEngine |
| `calibration/voice_calibrator.py` | Guided voice calibration for 3 conditions (good/flare/allergy); 20 phrases; voice-triggered or iPad Settings tab; writes to `voice_profile` + `voice_phrases` tables |
| `desktop/vision_grounder.py` | Local qwen3-vl:30b (Ollama) resolves named UI targets to pixel coords; claude-sonnet-4-6 cloud fallback via `core/cloud_backend.py` (Bedrock); confidence ≥0.7; 2s cache; fallback chain: vision → OCR → CLARIFY |
| `desktop/ui_automation.py` | Win32 UIAutomation BFS tree search; fuzzy name scoring; 0.3s timeout; 1s cache; first fallback in `_resolve_coords` |
| `desktop/action_verifier.py` | Pillow perceptual diff pre/post screenshot; verifies CLICK/OPEN/CLOSE/SCROLL; 2% pixel threshold; 400ms animation delay |
| `desktop/flick_engine.py` | Flick-to-snap gesture handler; maps GRAB_SNAP_* gestures to window snap zones; uses OneEuroFilter for smoothing |
| `desktop/target_cache.py` | `ClickableTargetCache` — daemon thread publishing a lock-protected snapshot of clickable UI targets for magnetic snap + cursor gravity; change-gated COM walk (foreground-hwnd + 1.5 s heartbeat), failure backoff, `CoUninitialize`; started behind `DA_CURSOR_GRAVITY` |
| `inference/kiro_client.py` | WebSocket client for Kiro/VS Code bridge extension on ws://127.0.0.1:8767; wired to DevAgent for code edits |
| `inference/codebase_indexer.py` | ChromaDB RAG index (cosine) over Python/Swift source + docs PDFs; accepts `embedder=`; oversized units sub-split into `_(i/N)` chunks (no 4000-char truncation); per-path debounced file watcher; time-gated `_available` re-probe; fed to DevAgent for context |
| `monitoring/metrics.py` | In-process metrics singleton; VRAM poller; optional `/metrics` HTTP endpoint |
| `monitoring/trace.py` | Cross-layer per-command tracing (enqueue→dispatch→route→inference→execute). On by default (opt out with `DA_TRACE=0`). Spans recorded at command boundaries (not the 60 Hz tick), flushed to `command_traces` fire-and-forget; pruned 30 days |
| `monitoring/replay.py` | `replay_trace(trace_id)` assembles commands + command_traces + inferences + event_log + audit_events into one ts-sorted timeline. Read-only. CLI: `python -m monitoring.replay <trace_id>` / `--recent N` / `--json` |
| `monitoring/trends.py` | Cross-session trend report over `session_summaries` — success/cloud/p50/p95/pain-day per session + recent-vs-older deltas. CLI: `python -m monitoring.trends [--db --limit --json]` |
| `monitoring/cost_ledger.py` | Rolls up cloud (Bedrock) token spend from `inferences` by model/day/session against a `$/MTok` price table (env-overridable `DA_BEDROCK_PRICES` / `DA_BEDROCK_PRICE_MULT`); local models = $0. CLI: `python -m monitoring.cost_ledger [--db --days --json]` |
| `storage/session_analyzer.py` | Post-session DuckDB analytics; route distribution, latency percentiles, error modes; summary persisted to AgentDB |
| `core/chat_server.py` | PC desktop chat UI server (`--chat`, aiohttp :8770, localhost). Chat + live DAG via EventBus keyed by trace_id. Also hosts the unified observability dashboard (`/dashboard` + read-only `/api/metrics`, `/api/recent-traces`, `/api/replay/{tid}`, `/api/trends`, `/api/cost`). Frontend in `web_client_chat/` |
| `core/scheduler.py` | `AccessibilityScheduler` — priority queue over `coordinator.route()`; 5 tiers (ACCESSIBILITY/VOICE/GESTURE concurrent, DEV_AGENT/BACKGROUND semaphore-gated); `fan_out()` runs independent sub-steps under a separate N=3 `_subagent_sem`; `pause_dev()`/`resume_dev()` flare admission gate; bounded queue (256) with priority-aware load-shedding of DEV/BACKGROUND only; `is_healthy()`/`restart()` for the Supervisor |
| `core/supervisor.py` | One-for-one liveness watchdog. Polls `is_healthy()` per subsystem; restarts a dead-but-`enabled()` loop under a bounded policy (≤5 restarts/60s → latch FAILED). On latch, fires `on_failed(name)` → main.py speaks TTS warning and degrades |
| `core/crash_marker.py` | Process-level unclean-exit detection: `check_and_mark()` writes `logs/agent.running` at pipeline start; `clear()` removes it on graceful shutdown. Complements `scripts/agent_watchdog.ps1` + `scripts/Install-AgentService.ps1` |
| `core/circuit_breaker.py` | Latched failure breaker — closed→open after N consecutive failures, fast-fails for `cooldown_s`, half-open probe on recovery. Wired into `OllamaInference` |
| `core/vram.py` | Single shared GPU-VRAM signal — `free_vram_gb()`/`used_vram_gb()`, 999.0 fail-open. `ModelRouter._free_vram_gb` delegates here |
| `core/vram_arbiter.py` | Single source of VRAM admission policy — `VramArbiter.can_admit(vram_gb, free_gb)` + `headroom_gb`, fail-open when unmeasurable. `ModelRouter.select_profile`/`get_status` route through it |
| `core/slo.py` | Per-domain routing SLOs — `SLOConfig` (per-domain latency budget + success floor; command=600ms), `evaluate(p50,success)→verdict`. `ContinuousTrainer._adapt_per_domain_slo()` logs per-domain breaches to `adaptation_log(domain)` + sets Gate-4 overrides |
| `inference/sandbox.py` | WSL2 namespace jail for RUN_TERMINAL — `run_sandboxed()` wraps the shell command in bubblewrap/firejail (cwd-jail, `--unshare-net`, RLIMIT_CPU/AS, output cap) when available, else graceful unsandboxed fallback. `DA_SANDBOX` flag (default on). Needs `apt install bubblewrap` in WSL |
| `storage/memory_manager.py` | `MemoryManager` — syscall façade over AgentDB + SemanticMemory: `read_context`/`write_state`/`search_semantic` + zero-copy `get_pain_day_active/score()`; `_VALID_KEYS` schema validation at write boundaries |
| `core/resource_governor.py` | Pain-aware kernel primitive; polls `MemoryManager.get_pain_day_score()` every 5s; on flare relaxes sensor thresholds, pauses indexer, raises Whisper VAD thread priority, evicts `qwen3-vl:30b` from VRAM, pauses scheduler dev/background admission; reverses on recovery (hysteresis 0.6/0.4) |
| `core/goal_session.py` | Goal-level authorization; one voice approval lets the goal's tool calls / DevAgent steps run silently; atomic-replace signal file `~/.claude/approval/goal_session.json` shared with `approval_hook.py`; `allows_action(tool, input)` adds path-scope + deny-by-default Bash allowlist; voice cancel/status/history |
| `core/cluster_health.py` | `ClusterHealthMonitor` — polls laptop service nodes (`laptop_ollama`/`whisper`/`indexer`); synchronous zero-cost `is_healthy()` for hot-path routing; fail-safe to local when unknown/down |
| `core/cluster_config.py` | `ClusterConfig` — laptop compute-node endpoints + offload policy (loads `cluster_config.json`); consumed by `model_router.py` and `cluster_health.py` |
| `core/async_utils.py` | `fire_and_log` — safe fire-and-forget for non-critical background DB writes (strong ref until done, DEBUG-logs exceptions, no-ops without a running loop) |
