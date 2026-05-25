Purpose

This file gives Copilot sessions and assistants focused, repo-specific operational guidance: how to build/run/tests, the high-level architecture, and project-specific conventions to follow when making changes.

1) Build, test, and (lack of) lint commands

- Python deps: create and activate a venv then install:
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt

- Run full test suite:
  pytest -q
  (pytest-asyncio is used; many tests are async.)

- Run a single test file or a single test function:
  pytest tests/test_some_file.py -q
  pytest tests/test_some_file.py::test_function_name -q
  pytest -k "substring" -q   # run tests matching substring

- Run core scripts / dev entry points:
  # Full pipeline (bridge + fusion + coordinator + trainer)
  python main.py [--port 8765] [--host 0.0.0.0] [--no-mdns] [--debug] [--safe-mode] [--viewer] [--viewer-only]

  # iPad bridge only
  python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

  # MCP server for Claude integration
  python mcp_server/desktop_mcp_server.py

- Measure VRAM (local model benchmarking)
  python benchmark_models.py --measure-vram

- Model pull (local Ollama models used during development):
  ollama pull llama3.1:8b
  ollama pull qwen3-coder:30b   # optional

- Linting/formatters: none configured in repo root. If you need to run formatters or linters, add them and CI hooks; no standard tool (black/ruff/mypy) is present.

2) High-level architecture (big picture)

- Sensors → iPad (Swift) → WebSocket → ipad_bridge.py
- FusionEngine (priority-based sensor fusion, 60Hz) routes commands + sensor-derived cursor motion
- WhisperStream handles PC-side transcription; GestureProcessor handles MediaPipe hand landmarks
- HybridCoordinator runs the 4-gate routing (privacy/local vs. cloud fallback) and chooses execution path
- CommandExecutor maps Command DTOs (16-verb vocabulary) to MCP tools → pyautogui / Win32 actions
- mcp_server/desktop_mcp_server.py exposes the actionable tools (mouse, keyboard, screen, windows, handwriting)
- ContinuousTrainer adapts thresholds and personalization; AgentDB (aiosqlite) + AnalyticsDB (DuckDB) hold persistent state
- Model routing: local Ollama by default; model selection is VRAM-aware via model_router.py

Key files to look at first: main.py, ipad_bridge.py, fusion_engine.py, hybrid_coordinator.py, command_executor.py, whisper_stream.py, gesture_processor.py, db.py, mcp_server/desktop_mcp_server.py

3) Key conventions and repo-specific patterns

- Command DTO / verb-first flow: every action is a Command dataclass and the system is verb-first (16 verbs: 11 accessibility + 5 dev-agent). Keep the verb vocabulary stable.

- Async-first design: most pipeline components are async. Use asyncio.to_thread for blocking I/O (Win32/pyautogui) to avoid blocking the loop.

- Sensor priority & gating: FusionEngine enforces an explicit priority list — iPad touch bypasses the LLM, gaze+voice/gesture map to clicks, tilt/head/gaze are separate cursor sources. Read fusion_engine.py to preserve semantics.

- SAFE_MODE and approval: SAFE_MODE (env or --safe-mode) prevents destructive tools during testing. approval_config.json and approval_hook.py implement voice approval flows and TTS gating.

- Audit-first: audit_log.py implements an append-only audit.db (WAL mode). Many components log security events; do not change audit schema lightly.

- MCP tool outputs are scanned by MCPTrustClassifier before entering LLM reasoning — treat tool outputs as untrusted data.

- Model setup: local inference expects Ollama models; CI/devs should `ollama pull` before running model-dependent paths.

- Tests: tests rely on pytest-asyncio. Many tests are integration-style and may assume a Windows environment (Win32 UI automation); run unit tests selectively when not on Windows.

- iPad app: native Swift project is under iPadApp/ and is built in CI via GitHub Actions. See .github/SIGNING_SETUP.md for TestFlight signing steps.

Useful files and sidecars

- .github/SIGNING_SETUP.md — iPad app signing & TestFlight CI
- requirements.txt — pinned Python deps (faster-whisper, mediapipe, aiohttp, pyautogui, aiosqlite, etc.)
- CLAUDE.md and README.md — rich status, run commands and architecture notes
- approval_config.json — runtime approval/TTS config (voice, device names)

Contributing notes for Copilot

- Prefer edits that preserve async/await contracts and avoid blocking the event loop
- When changing command verbs, update all dispatch sites (Command consumers, mcp tools, approval hooks, tests)
- If adding new external dependencies, update requirements.txt and run the test suite locally

Where to look for more details

- .kiro/specs/... for design docs and diagrams
- docs/ for daily reviews and architecture ADRs
- tests/ for example usages and expected behavior patterns

GitHub Actions

- CI workflows live in .github/workflows/. Notable workflow(s):
  - .github/workflows/build-ipad-app.yml — builds the iPad Swift project and (optionally) uploads to TestFlight; it requires signing secrets (CERTIFICATE_P12, CERTIFICATE_PASSWORD, KEYCHAIN_PASSWORD, PROVISIONING_PROFILE, TEAM_ID, ASC_*). See .github/SIGNING_SETUP.md for the signing guide.

- For test/CI steps: inspect each workflow in .github/workflows/ for project-specific test invocations (some workflows may run Swift builds only). Copilot assistants should consult workflows before adding CI-related changes.

Build-iPad workflow behaviors (expanded)

- Triggers: push to iPadApp/** and workflow_dispatch with deploy_testflight input (default "true").
- Runner: macos-26 with a 30 minute timeout.
- Steps: checkout repo, select Xcode, print Xcode version, install XcodeGen.
- Signing: create a temporary keychain and import CERTIFICATE_P12 (base64). Sets key partition list for CI signing.
- Provisioning: decode PROVISIONING_PROFILE secret and install it to ~/Library/MobileDevice/Provisioning Profiles; workflow extracts the profile UUID and writes it to GITHUB_ENV.
- Project generation: xcodegen generates the Xcode project and the workflow stamps CFBundleVersion with the run number.
- Build & archive: uses xcodebuild archive (manual signing) and validates success by scanning build.log.
- Export IPA: uses xcodebuild -exportArchive with a generated ExportOptions.plist.
- TestFlight upload: conditional (push OR deploy_testflight == 'true'); uploads with API key auth (ASC_KEY_ID/ASC_ISSUER_ID/ASC_PRIVATE_KEY); step is continue-on-error to tolerate App Store rejections.
- Artifact upload: uploads the signed IPA as a workflow artifact (upload-artifact), marked continue-on-error for transient runner issues.
- Cleanup: always-step deletes the temporary keychain.

Secrets referenced by the workflow: CERTIFICATE_P12, CERTIFICATE_PASSWORD, KEYCHAIN_PASSWORD, PROVISIONING_PROFILE, PROVISIONING_PROFILE_NAME, TEAM_ID, ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY.

End

