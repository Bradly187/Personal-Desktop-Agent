# Personal Desktop Agent — Development Roadmap

> **⚠️ Stale (last updated 2026-05-25).** Predates Sprints N/O/P/Q, the audit hash-chain, Gmail
> OAuth, the skill model/breadth, proactivity, the audio/voice pipeline, and the eval harness.
> For the current backlog see `CLAUDE.md` and `docs/audits/2026-06-14-audit-and-sprint-plan.md`.

*Last updated: 2026-05-25*

This roadmap covers the next wave of improvements identified from a hardware resource analysis
and commercial gap analysis. Items are ordered by impact-to-effort ratio.

---

## Status Key

| Symbol | Meaning |
|--------|---------|
| ✅ | Complete — merged to master |
| 🔄 | In progress |
| ⏳ | Pending |

---

## Phase 7 — Hardware & IDE Integration

### #1 — Fix vLLM CUDA wheels ✅
**ETA:** 1 hour | **Impact:** ~10× inference throughput (100 → 1,186 tok/s)

**Verified 2026-05-29:** vLLM 0.21.0 + torch 2.11.0+cu128 running in Ubuntu WSL2 on RTX 5090 (sm_120).
`Meta-Llama-3.1-8B-Instruct` serves successfully — cold load 50s, responds correctly.

**Production deployment notes (WSL):**
- Server must be started from within WSL (automount disabled in `wsl.conf`)
- `--gpu-memory-utilization 0.65` when Whisper is also loaded (~4.2 GB); `0.75` standalone
- The ninja JIT build tool must be on PATH — copy is at `~/.local/bin/ninja`
- Activate with `--backend vllm` flag in `main.py`

Speculative decoding (item #9) now unblocked.

---

### #2 — Kiro IDE Extension + `kiro_client.py` ✅
**ETA:** 2–3 days | **Impact:** Closes Gap 1 (IDE blindness) entirely

TypeScript VS Code extension (`kiro-extension/`) exposes a WebSocket server on port 8767.
Python `kiro_client.py` connects to it and gives the entire Python pipeline access to:
- Active file, cursor position, selected text, surrounding context (50 lines above/below)
- Language server diagnostics at cursor
- Git branch, staged/unstaged changes, last commit via VS Code's built-in git extension
- `apply_edit()` — writes to editor buffer with undo history + LSP validation
- `run_terminal()` — sends commands to integrated terminal
- `open_file()` — opens file at specific line

**Files created:**
- `kiro-extension/package.json` — VS Code extension manifest
- `kiro-extension/tsconfig.json` — TypeScript config
- `kiro-extension/src/extension.ts` — WebSocket bridge server
- `kiro_client.py` — Python async client

**Wire-up:** `main.py --kiro` → creates `KiroClient` → passed to `DevAgent.set_kiro()`.

---

### #3 — Git-native DevAgent actions ✅
**ETA:** 2 hours | **Impact:** Direct Devin-parity; removes brittle screen-scraping for git

New plan step verbs added to `DevAgent`:
- `GIT_STATUS` — runs `git status --short`, returns staged/unstaged file list
- `GIT_DIFF` — runs `git diff [--staged]`, injects into plan context
- `GIT_COMMIT` — runs `git commit -m "..."` with voice-approved message
- `GIT_CHECKOUT` — `git checkout [-b] <branch>`
- `GITHUB_PR` — `gh pr create --title ... --body ...`, returns PR URL (spoken back)
- `FETCH_URL` — HTTP GET + text extraction; replaces `SEARCH_WEB` browser-open for retrieval

Also: git context (branch, staged changes) auto-injected into every `plan_and_run()` prompt.

---

### #4 — RAM disk for hot data paths ✅
**ETA:** 30 minutes | **Impact:** Eliminates DB write latency from command path

`setup_ramdisk.bat` — creates 32 GB ImDisk RAM disk (R:\), symlinks `agent.db` and
`chroma_db/` to it, and sets up a scheduled task to rsync back to SSD on shutdown.

Manual steps documented in `docs/ramdisk_setup.md`.

---

### #5 — File watcher for incremental RAG indexing ✅
**ETA:** 1 day | **Impact:** Keeps codebase RAG current without startup delay

`watchdog` library integration into `CodebaseIndexer`:
- `start_watching()` — background `Observer` thread watching `.py`/`.swift` files
- On change event: `asyncio.run_coroutine_threadsafe()` → re-index only changed file
- `ProcessPoolExecutor` (4 E-core workers) for AST parsing + chunking (CPU-bound, off event loop)
- `--watch` flag in `main.py` enables continuous watching (default: off; `--index-codebase` is still one-shot)

---

### #6 — LlamaCppInference backend + Qwen3.6-27B ✅
**ETA:** 1–2 days | **Impact:** 68.9% SWE-Bench at 158 tok/s locally; 27B parameter quality

`LlamaCppInference` added to `local_inference.py`:
- Uses `llama-server` OpenAI-compatible HTTP endpoint (`http://localhost:8080/v1`)
- `--n-gpu-layers` split: full 27B model fits in VRAM at Q4_K_M (17 GB); 72B models can split to RAM
- Activated via `--backend llamacpp` flag in `main.py`
- `llama_server_setup.md` documents model download + server launch command

---

### #7 — ProcessPoolExecutor for CPU workers ✅
**ETA:** 1 day | **Impact:** Proper use of E-cores; removes AST/FFT from event loop

Implemented as part of `CodebaseIndexer` (chunking) and `AcousticProfiler` (FFT spectral centroid).
- `ProcessPoolExecutor(max_workers=4)` shared singleton in `codebase_indexer.py`
- `asyncio.get_event_loop().run_in_executor(_cpu_pool, chunk_fn, path)` for each file
- PDF chunking also moved to process pool

---

### #8 — Git context injection into plan prompts ✅
**ETA:** 2 hours | **Impact:** LLM sees branch/diff state; avoids overwriting staged work

`DevAgent.plan_and_run()` now calls `_git_context()` before generating the plan.
- Reads branch name, ahead/behind, staged/unstaged files via `git status --short` + `git log`
- If `KiroClient` is wired, uses `get_git_state()` (richer data from VS Code git extension)
- Formatted as a fenced block prepended to the system prompt

---

### #9 — Speculative decoding ⏳
**ETA:** 1 hour (after #1) | **Impact:** 2–3× effective throughput on code generation

vLLM server flag: `--speculative-model llama3.1:8b` with qwen3-coder:30b as draft target.
Acceptance rate for code: 60–80%. Effective throughput: ~2,400–3,500 tok/s.

Requires #1 (vLLM baseline) to be confirmed working first. Add to `vllm_setup.bat` after baseline test passes.

---

### #10 — GPU-accelerated embeddings ✅
**ETA:** 10 minutes | **Impact:** RAG retrieval 80ms → 8ms; free win on existing stack

`SentenceTransformerEmbeddingFunction(device="cuda")` in `codebase_indexer.py` and
`semantic_memory.py`. Falls back to CPU if CUDA is unavailable.

---

## Completed (Phases 1–6)

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | iPad bridge, MCP server, command executor | ✅ |
| 2 | FusionEngine, HybridCoordinator, LocalInference | ✅ |
| 3 | GestureProcessor, LiDARReceiver, DomainClassifier, ModelRouter, DevAgent | ✅ |
| 4 | ContinuousTrainer, main.py, benchmark, WhisperStream, AgentDB | ✅ |
| 6 | Cloud fallback (Bedrock, Transcribe, Polly), Chatterbox TTS | ✅ |
| A | AcousticProfiler — VAD calibration, drift detection | ✅ |
| B | iPad accessibility onboarding UI (VoiceProfilingSheet, etc.) | ✅ |
| C | Continuous recalibration, ipad_bridge pain_day_override | ✅ |
| 5 | VisionGrounder (claude-sonnet-4-6 vision API) | ✅ |
| 6 | UIAutomation Win32 BFS tree search | ✅ |
| 7 | ActionVerifier perceptual diff | ✅ |
| G1–G4 | Gaze monitor calibration (5-point affine, overlay UI) | ✅ |
| iPad logs | Structured AppLogger forwarding over WebSocket | ✅ |
| ML obs. | metrics.py, session_analyzer.py, codebase_indexer.py, dashboard.py | ✅ |

---

## Future Considerations

- **Sandboxed RUN_TERMINAL** — Docker container or restricted PowerShell runspace; voice-approve gate for destructive commands
- **Commercial roadmap** — StoreKit subscription, multi-user support, cloud inference at <$0.10/user/day
- **Real RealSense D435i** — purchase ~2026-05-31 to replace current iPad LiDAR simulation
