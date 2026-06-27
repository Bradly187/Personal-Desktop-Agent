# Personal Desktop Agent

@AGENTS.md

<!-- ^ Shared cross-tool behavior rules (also read natively by Antigravity).
     Keep behavioral rules in AGENTS.md, not here, so both IDEs stay in sync. -->

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, hand gesture, iPad tilt, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). (Eye-gaze and head-pose control were removed — the standard iPad lacks the required TrueDepth sensor.) Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `specs/steering/tech.md`
- Open tasks: `specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/daily/`

## Current Status (2026-06-24)

> **Schema fact (authoritative):** `agent.db` = **49 tables** at `PRAGMA user_version = 8` (`storage/db.py` is the schema source of truth — count verified 2026-06-24 by `CREATE TABLE IF NOT EXISTS` in `storage/db.py`, excluding the 3 DuckDB `benchmark_*` tables; v8 added the `commands.resolved_by` CLICK-resolver-tier column, not a table; **2026-06-25 added the `agent_workflows` table — a pure additive `CREATE TABLE IF NOT EXISTS`, so user_version stays 8, no migration**); `AnalyticsDB` (DuckDB) holds the **3** `benchmark_*` tables. Table counts in `docs/CHANGELOG.md` and older docs are historical (as-of-their-date), not current.

Phases 1–6 + Sprints A–C / 5–7 / G1–G5 / N–Q shipped and merged. Full dated history → [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Day-by-day notes → `docs/daily/`.

## Run Commands

```bash
# Full pipeline — bridge + FusionEngine + HybridCoordinator + ContinuousTrainer
python main.py [--port 8765] [--host 0.0.0.0] [--no-mdns] [--debug] [--safe-mode] [--viewer] [--viewer-only]

# Measure actual VRAM usage on RTX 5090 (loads all models, prints table, exits)
python main.py --measure-vram

# MCP server — Claude's desktop control interface (stdio transport)
python mcp_server/desktop_mcp_server.py

# iPad WebSocket bridge (standalone, without FusionEngine)
python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

# End-to-end integration test (start bridge first in another terminal)
python tests/test_bridge_client.py

# Install dependencies
pip install -r requirements.txt
```

Set `--safe-mode` (or `SAFE_MODE=1`) to block `keyboard_type` and `mouse_drag` during testing.

## Action Vocabulary

**Accessibility verbs (11)** — for iPad sensor pipeline and simple commands:
`CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY` `DICTATE` `CLARIFY` `SCREENSHOT`

`MOUSEDOWN`/`MOUSEUP` are executed synchronously (no `asyncio.to_thread`) because they are timing-critical for drag-select and must not compete with trackpad moves.

**Dev-agent verbs (5, via `CommandExecutor`)** — the 5 dev verbs `CommandExecutor` dispatches alongside the 11 accessibility verbs (16 total):
`WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

**Planner-only verbs (8, `DevAgent` internal)** — emitted by the DevAgent planner and resolved directly within `DevAgent` via subprocess; never reach `CommandExecutor`:
`SPAWN_PROCESS` `READ_STREAM` `SEND_INPUT` `TERMINATE_PROCESS` `GIT_CREATE_BRANCH` `GIT_CHECKOUT` `GIT_COMMIT` `GIT_DIFF`

`SNAP_WINDOW` is an additional `CommandExecutor` verb for D7 flick-to-snap gestures (not part of the planner vocabulary).

The `CommandExecutor` handles 16 verbs (11 accessibility + 5 dev). The `DomainClassifier` determines which pipeline a query enters — accessibility (llama3.1:8b, verb-first) or dev-agent (specialist model, free-form).

## Architecture

```
iPad sensors  → WebSocket :8765 → ipad_bridge → FusionEngine → HybridCoordinator ─┐
                                                                                    │
                                               DomainClassifier                     │
                                              /               \                     │
                                       command domain       dev domains             │
                                             │           (CODE/MATH/VISION/         │
                                        llama3.1:8b       PLAN/GENERAL)            │
                                        verb-first         ModelRouter              │
                                             │            specialist LLM            │
                                             └──────────────────┘                  │
                                                      │                             │
                                               CommandExecutor                      │
                                            (16 verbs: 11 access + 5 dev)          │
                                                      │                             │
                                         mcp_server/tools/ → pyautogui / Win32 ←──┘

Claude (MCP) → stdio → mcp_server/desktop_mcp_server.py → mcp_server/tools/
```

Every pipeline boundary carries a `Command` dataclass. `DomainClassifier` gates the pipeline: simple commands go straight to `llama3.1:8b`; dev-domain queries go to `DevAgent` which selects the right specialist model.

## Key Files

> Full annotated file map: [docs/file-map.md](docs/file-map.md)

## Sensor Priority (FusionEngine — `core/fusion_engine.py`)

6-level priority (gaze, head-pose, and mouth-sound control all removed):

1. iPad touch command — bypasses LLM entirely
2. Voice "click" keyword — clicks at the current cursor position (bypass, source `multimodal`)
3. Tilt navigation (Core Motion) — 3a absolute position, 3b legacy velocity
4. Gesture alone
5. On-device voice keyword (Speech Framework)
6. PC-transcribed voice (Whisper large-v3 on GPU)

## TTS

> Full reference (voices, paths, engines, mic approval flow): [docs/tts.md](docs/tts.md)

**Default runtime backend: Kokoro** (local ONNX, `tts_backend: "kokoro"` in `approval_config.json`, voice `af_bella`, runs on CPU — GPU auto-selects when `onnxruntime-gpu`+CUDA present). Switch backends via `tts_backend` (`kokoro` | `polly` | `chatterbox` | `sapi`); change the Polly voice via `"voice_id"` (default **Danielle**, en-US Generative 24 kHz). Both take effect immediately, no restart. **Note:** `approval_hook.py`'s voice-approval consent prompts still speak via Amazon Polly directly (hardcoded `_polly_speak`), independent of `tts_backend` — only the agent runtime honors the backend switch.

## WebSocket Protocol

> Full message-type reference: [docs/websocket-protocol.md](docs/websocket-protocol.md)

iPad → PC (26 types): `tilt` `tilt_position` `tilt_tap` `tilt_ratchet` `keyword` `audio_stream` `camera_frame` `depth_frame` `touch_command` `trackpad` `handwriting_image` `dwell_click` `ping` `set_dwell_action` `set_feature_toggle` `sensor_switch` `cursor_pause` `cursor_resume` `gesture_assessment` `pain_day_override` `flare_profile` `calibration_start` `calibration_cancel` `mic_mute` `a2ui_event` `ipad_log`

PC → iPad (12 types): `ack` `pong` `status` `screenshot` `handwriting_result` `recalibration_request` `mic_state` `calibration_result` `calibration_phrase` `calibration_complete` `calibration_error` `a2ui_clear`

## Coding Conventions

- All pipeline classes are `async`; blocking I/O uses `asyncio.to_thread`
- Every sensor class must degrade gracefully — wrap hardware imports in `try/except ImportError`, log a warning, never crash
- No global state outside dataclass instances; all state lives in class attributes
- `Command` is the universal DTO — never pass raw dicts across pipeline boundaries
- Log levels: DEBUG per-frame, INFO commands/routing, WARNING sensor failures, ERROR unrecoverable

## Known Gotchas

- **Voice approval gate requires an explicit confirmation word.** While `approval_hook.py`'s `~/.claude/approval/pending` file exists, `WhisperStream._handle_approval_gate()` writes a response ONLY when the transcript classifies as a deliberate approve/deny (`core/approval_keywords.classify_confirmation` — single source of truth shared with `approval_hook.py`). Ambient audio / podcast speech / a stray word returns `None` → discarded, gate keeps waiting. Deny wins ties; utterances longer than `MAX_ANSWER_WORDS` (6) are treated as ambient. The TTS echo is suppressed for 1.0s so Danielle's spoken "Approve …?" can't self-approve. Timeout/ambiguity/silence **fail safe to DENY**.

- **Domain-classifier learning is experimental and OFF by default (`DA_DOMAIN_LEARN`).** With the flag unset, `DomainClassifier` is the static-keyword classifier and the `router_domains` eval baseline holds. When on, `ContinuousTrainer._learn_domain_overlay` learns per-domain vocabulary into `domain_keyword_weights` (bounded nudge, capped at `_MAX_OVERLAY_NUDGE=15`, never overrides static scores). Rollback: misroute rate rise clears that domain's overlay.

- **VRAM model roster (RTX 5090; baseline/Whisper measured 2026-05-08, roster current):** baseline 8.3 GB, Whisper +4.2 GB, ~19 GB free for LLM. Default command domain: `llama3.1:8b` (4.6 GB). Specialist models (source of truth: `inference/model_router.py`): `qwen3-coder:30b` (code+plan, thinking ON), `deepseek-r1:8b` (math, chain-of-thought kept), `qwen3-vl:30b` (vision), `gemma4:12b` (general, ~9.1 GB; co-resides with command+Whisper since 2026-06-07 — `gemma3:27b` is **retired**, kept pulled for rollback; flare fallback `gemma4:e4b-it-qat`). `llama3.1:70b` does not fit alongside Whisper. `nemotron-mini` (25%) and `gpt-oss:20b` (0%) were removed. `deepseek-r1:8b` reasoning output is kept for math but is incompatible with verb-first command format.

- **DevAgent trajectory reduction is experimental and OFF by default (`DA_TRAJECTORY_REDUCE`).** Unset = byte-identical legacy trajectory render. When on, `inference/trajectory.render_trajectory` compacts the re-sent trajectory (recent 3 steps verbatim, older successes abstracted, older read-only runs collapsed) to cut replan/reflect tokens — deterministic, no LLM, and **ALWAYS preserves failure signal**. **Held OFF by decision (2026-06-24):** baseline passes its gate but carries a documented ~12.5pt recovery-ordering cost on long-prefix builds — a deliberate trade-off hold, not a missing gate. Spec: `specs/trajectory-reduction/`.

- **WRITE_FILE is lint-gated; edit format is a per-model knob (default `whole_file`).** Every `WRITE_FILE` routes through `inference/edit_format.py` `EditApplier.apply()` *before* touching disk: it builds the result for the format, then runs the registered validator (`.py` → `ast.parse`). A broken result raises `EditError` (**fail-closed — file untouched, no compensation**) and the diagnostic feeds the replan loop. Format resolves per plan-model via `ModelRouter.edit_format_for()` (config `edit_format_aci.per_model`). **Default `whole_file` everywhere — byte-identical to legacy except broken Python is now rejected pre-write.** `hashline` and `udiff` (no-line-number unified diff — implemented 2026-06-25, layered exact→ws→fuzzy match, atomic bottom-up + overlap-reject, fail-closed; `UDIFF_PROMPT_INSTRUCTIONS` injected per-model) are both implemented but opt-in per configured model. A/B gate kept `whole_file` (the structured formats are efficiency plays, not correctness upgrades). Unknown formats still degrade to whole_file. **`EDIT_FILE` verb (2026-06-25, R5) = Claude-Code-parity surgical edits:** a separate destructive verb (distinct from WRITE_FILE) available to *every* plan model, body is aider-style `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks applied via the `search_replace` format. **Fail-closed** if a SEARCH isn't found EXACTLY ONCE (stale or ambiguous → `EditError`, file untouched); same `_lint` gate, Critic, `_snapshot_for_write` saga, and Tester path as WRITE_FILE. Empty SEARCH = creation on an empty file only. Planner is told to prefer EDIT_FILE for targeted changes, WRITE_FILE for new/whole-file rewrites. Spec: `specs/edit-format-aci/` (R5, tasks 8–9).

- **Plan-parse auto-repair is ON by default (`DA_PLAN_REPAIR`, flipped 2026-06-24; `=0` for byte-identical legacy).** Off = an unknown-verb step is silently dropped and a fully-unparseable plan wraps as one EXPLAIN. On (default), `DevAgent._acquire_plan_steps` re-prompts the planner with a corrective message (names the dropped verb + restates the schema) up to `DA_PLAN_REPAIR_MAX` (default 1) times, then fails safe. Deterministic; **never executes a partial/guessed plan**. Backend-agnostic. Spec: `specs/dev-agent-plan-contract/`.

- **Independent Critic + autonomous Tester on WRITE_FILE are ON by default (`DA_CRITIC`, `DA_TESTER`, flipped 2026-06-24; either `=0` for byte-identical legacy).** **Critic:** after the lint gate, `inference/critic.py` reviews the diff on the already-loaded plan/general model with a fresh reviewer context (**no generator CoT, no new VRAM — AGENTS.md #6**). PASS commits; a low-confidence PASS (`<DA_CRITIC_FLOOR`, default 0.6) **only ADDS** a confirm (never weakens a gate); REVISE/BLOCK drives `_replan` (no snapshot/compensation, bounded by `DA_CRITIC_MAX_REVISIONS`). Conservative parse (unparseable→REVISE; security/correctness finding floors PASS→REVISE); a Critic error **fails safe to escalate-confirm, never silent auto-approve**. **Tester:** after a committed `.py` SOURCE write, `inference/tester.py` generates+runs a focused pytest one-shot via `inference/sandbox.run_sandboxed`; the outcome is appended as a **safe-observation** — a failing generated test feeds `_reflect`/replan but the good write is **NEVER rolled back**. Skip-on-flare; never reports a skip as a pass. Spec: `specs/dev-agent-critic/`.

- **DevAgent saga rollback already exists; two enhancements added 2026-06-27.** A durable **per-step compensation saga** (NOT forward-only) is the long-standing E2/E3/E5/E6 behavior: `_snapshot_for_write` backs up WRITE_FILE/EDIT_FILE pre-write, `saga_compensations` rows are registered per successful destructive step, and `_halt_and_compensate`/`_run_compensations` unwind them in reverse on replan-exhaustion, max_steps, and user-cancel (RESTORE_FILE / DELETE_FILE / REVERT_TERMINAL-manual-note). The Antigravity `specs/dev-agent-sagas/` draft (atomic multi-file rollback) was **closed as already-implemented** — do NOT rebuild it, and preserve its two intentional non-goals: **Critic REVISE doesn't snapshot** (runs pre-disk-commit) and **Tester failure never rolls back the good write** (safe-observation → replan). Two close-out gaps were then shipped: **(1) `DA_SAGA_ANNOUNCE` (default ON)** — a rollback now speaks a short TTS summary ("Reverted N file changes."), closing the previously-silent user-cancel path; `_run_compensations` records `{reverted,manual,incomplete}` in `_rollback_summary`, `_rollback_notice()` renders it, `=0` → byte-identical legacy speech. **(2) `DA_SAGA_GIT_BACKEND` (default OFF)** — opt-in git-blob snapshot backend: pre-write bytes captured via `git hash-object -w` (no 256 KB cap, closes the file-copy large-file gap), restored via `git cat-file blob`; touches ONLY the object store (never working tree/index/stash), self-describing restore, degrades to file-copy outside a repo. A whole-tree `git stash` was deliberately avoided (would stash the user's unrelated work). Spec: `specs/dev-agent-sagas/`. Tests: `test_saga_enhancements.py` (14).

- **WSL terminal routing is ON by default (`wsl_terminal_routing.enabled`, since 2026-06-21).** On a Windows host the bwrap/firejail jail (`inference/sandbox.py`) can't apply natively, so RUN_TERMINAL would fall through to the unsandboxed allowlist-only path. WSL routing closes that gap: a **WSL-safe** command runs inside WSL2 via `wsl.exe -e <bwrap-jail>` so the jail actually applies. **Scope-preserving:** `_path_in_scope` runs upstream on the Windows path; `to_wsl_path` is a 1:1 `E:\…`→`/mnt/e/…` map that **refuses** UNC/non-drive paths (→ native, never a guess). Windows-only commands (PowerShell/`cmd`/`*.exe`/drive-anchored/unknown-exe under default `unknown_command_policy=native`) stay native. Decision order WSL→native, **degrading gracefully** (WSL/bwrap absent or untranslatable → native, logged). `enabled: false` to opt out. Spec: `specs/wsl-terminal-routing/`.

- **`pyautogui.typewrite` is ASCII-only** — `TYPE` keeps this limitation for backward compat. `DICTATE` uses `keyboard_paste()` (win32clipboard + Ctrl+V) and supports full unicode.

- **Voice conversation mode is experimental and OFF by default (`conversation_mode.enabled` in `~/.claude/ipad_bridge/config.json`).** With the flag unset the voice path is byte-identical legacy. When on, `core/conversation_mode.ConversationMode` adds a wake/sleep-gated **talk-only** dialogue: a voice utterance matching a **wake phrase** ("let's talk") enters the mode (intercepted in `HybridCoordinator._route_impl` *before* the dev pre-gate); while active, every utterance that isn't a **sleep phrase** ("that's all") is answered by the resident **general** model (`ModelRouter.infer(domain="general")` → gemma4:12b — no new VRAM, AGENTS.md #6) with the running history threaded in, and spoken via the configured TTS backend. The command/dev pipeline is **bypassed** for conversational turns (v1 = pure talk; acting mid-dialogue is a deferred non-goal). Detection is **anchored equality** (normalize → drop filler → set membership), deliberately conservative so "how do you say goodbye in French" never exits — and pure/deterministic so it's tick-safe (#2). **Critical detail — feedback-loop guard:** `_speak_and_suppress` suppresses the mic (`WhisperStream.suppress`) for the estimated playback duration *before* speaking plus an echo tail *after*, so the agent never transcribes its own TTS as the next turn. **Fail-safe (#4):** ambiguous detection or any handler error → mode unchanged, falls through to ordinary command routing. Spec: `specs/conversation-mode/`. Tests: `test_conversation_mode.py` (40).

- **Self-skilling (macros) is experimental and OFF by default (`self_skilling.enabled`).** Rung 2 of the self-skilling ladder: the agent crystallizes recurring multi-step plans into named, replayable macros. When on, `adaptive/macro_detector.py` mines successful trajectories **offline** (never the 60 Hz path — AGENTS.md #2; skipped during a flare — AGENTS.md #5), deterministic/NO LLM, and only **announces** a candidate. The voice phrase `"save that as a command called X"` (`core/macro_store.parse_macro_save`) is the **only** promotion path — **fail-safe-DENY, nothing enabled on silence** (AGENTS.md #4). `core/macro_store.MacroStore` replays a promoted macro through the normal `CommandExecutor` (every gate fires) and verifies **all** constituent tools exist before executing **any** step, else fails safe with CLARIFY (**never a partial macro**). **Rung 3 (drafting, human-gated) and rung 4 (autonomous authoring) are explicit non-goals — rung 4 is forbidden by the spec.** Spec: `specs/self-skilling/`.

- **First-class `grep` / `glob_files` / `fetch_url` MCP tools (2026-06-25, Claude-Code parity).** Code search + web fetch used to be DevAgent *plan verbs* only; now they're direct MCP tools (`mcp_server/tools/search.py` + `web.py`, registered in `desktop_mcp_server.py`). `grep` + `glob_files` are **read-only but scoped to the writable-root allowlist** (`_load_writable_roots`) via `_path_in_scope` — a direct call **can't read outside writable_roots** (deny-by-default, fail-closed if the resolver is unavailable). `DevAgent._grep` delegates to the same `search.search_text` (parity; in-process verb passes `scopes=None` to keep its repo-wide read). `fetch_url` is **http(s)-only** (other schemes refused) and its output is run through the existing `MCPTrustClassifier` in `call_tool`. **Deferred:** command-domain web *enrichment* in `HybridCoordinator` (Privacy-Gate-0-gated) and sharing the async `DevAgent._fetch_url`. Spec: `specs/first-class-search-tools/`.

- **LSP Semantic Navigation (2026-06-26).** First-class `lsp_get_definition` and `lsp_find_references` MCP tools provide AST-aware code navigation (Go-To-Definition, Find-References). Wrapped via a lightweight stdio JSON-RPC client to a background Python language server (e.g., `pyright-langserver`) in `mcp_server/tools/lsp.py`. Drastically reduces symbol hallucination compared to raw grep. Spec: `specs/dev-agent-lsp-tools/`.

- **Multi-agent workflow orchestration is experimental and OFF by default (`workflow_orchestration.enabled`).** `inference/workflow.WorkflowRunner.fan_out` runs N **fresh-context** sub-agent inference calls (`ModelRouter.infer(domain, context="")` — the Critic/Tester pattern, **no new model loaded**, AGENTS.md #6) concurrently via the scheduler's deadlock-free `fan_out`, with an optional **adversarial verify** pass (strict YES/NO judge, **fail-safe to NOT-verified** on any error/ambiguity). It does **not** wrap `DevAgent.plan_and_run` (serialized by `_plan_lock`; the single GPU serializes inference anyway) and executes **no** desktop/file/shell actions — pure inference orchestration, no new gate bypass. **Skip-on-flare** (AGENTS.md #5); **never on the 60 Hz path** (#2). Runs are journaled to the additive `agent_workflows` table (CREATE TABLE IF NOT EXISTS — no `user_version` bump). **Live voice trigger:** a voice utterance leading with `"think hard about …"` / `"research …"` / `"brainstorm …"` (`core/workflow_voice.parse_workflow_request` — anchored + non-empty goal, deterministic) intercepts in `HybridCoordinator._maybe_handle_workflow` **before** the dev pre-gate, decomposes the goal into N sub-angles, runs `fan_out`, then synthesizes + **speaks** one answer (with the `_speak_and_suppress` mic-feedback guard). Pure talk — no desktop actions; bypasses the command/dev pipeline for a handled request. **Fail-safe (#4):** non-match / flare / missing-router / any error → `None` → ordinary routing; decompose failure degrades to a single sub-agent. Gates on the same `workflow_orchestration.enabled` flag (default OFF). **Still deferred:** `pipeline` (per-item staged) mode; an MCP-tool trigger is ruled out (the MCP-server process has no `ModelRouter` — driving Ollama there bypasses the VRAM-eviction lifecycle, #6). Spec: `specs/workflow-orchestration/`.

- **Browser/UI testing `preview_*` MCP tools (2026-06-25, Claude-Code parity) — Playwright is an OPTIONAL dep.** `mcp_server/tools/browser.py` adds `preview_start` / `preview_screenshot` / `preview_snapshot` / `preview_click` / `preview_fill` / `preview_console_logs` / `preview_network` / `preview_stop` for verifying web UIs (target: `core/chat_server.py` :8770 + `web_client_chat/`). **Degrades gracefully** — Playwright absent → every tool returns `{"ok": False, "disabled": True, …}` with the install hint (`pip install playwright && playwright install chromium`), never crashes. **Event-loop isolation:** Playwright's sync API can't run inside the server's asyncio loop, so the session lives in a dedicated worker thread (`_BrowserWorker`) that also persists the page across calls. `preview_start` **refuses non-localhost URLs** unless `allow_external=true`; `preview_click`/`preview_fill` are **SAFE_MODE-gated** (mutate page state). Spec: `specs/browser-ui-testing/`.

- **Mini-coding-agent gap closure (Gaps A–D) features are ON by default.**
  - **Live repo-context ingestion (`DA_REPO_CONTEXT`):** Builds stable repo facts (AGENTS.md/CLAUDE.md, layout, git branch/log) once and injects them ahead of dynamic RAG/git context.
  - **Trajectory read-dedup (`DA_TRAJECTORY_DEDUP`):** Drops superseded duplicate reads from the trajectory sent to the model (independent of reduction).
  - **Resume working-memory (`DA_RESUME_MEMORY`, default ON):** On crash-resume, `DevAgent._resume_seed_context` derives a compact `{files,notes,last_failure}` from the interrupted run's `agent_steps` and seeds the resumed plan (`<resumed-task-memory>` block). Default flipped ON 2026-06-26 (5b integration test confirms the flag-off path is byte-identical).
  - **Cross-session working-memory (`DA_SESSION_MEMORY`, default OFF) — the address to the session-level context-loss ("Codified Context") gap.** When ON, a *fresh* plan with no caller seed pulls compact working-memory from recent *related* prior runs: `select_related_runs` (Jaccard goal overlap — deterministic, no LLM/embeddings) picks the top-K, and `DevAgent._session_seed_context` renders a `<prior-session-memory>` block (distinct tag) ahead of RAG/git. Mutually exclusive with crash-resume seeding (a caller `seed_context` wins — never double-seeds). Schema-free (read-only `AgentDB.get_recent_runs` SELECT — no `user_version` bump), bounded work once per plan, off the 60 Hz path, no new VRAM, degrades to `""` on any error. Default OFF until relevance quality is validated on real back-to-back runs. Spec: `specs/resume-working-memory/` (R4).
  - **Planner DELEGATE verb (`DA_DELEGATE`):** `[DELEGATE <q>]` spins a bounded read-only investigation sub-agent whose finding returns into the trajectory. Depth-capped, fail-safe to skip on flare, runs sequentially.
  To revert to legacy behavior (byte-identical), set these flags to `0` or `false`. Specs: `specs/repo-context-ingestion/`, `specs/trajectory-read-dedup/`, `specs/resume-working-memory/`, `specs/dev-agent-delegate-verb/`.

## MCP Server Registration (Claude Code)

Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "desktop-agent": {
      "command": "python",
      "args": ["E:/Personal_Desktop_Agent/mcp_server/desktop_mcp_server.py"]
    }
  }
}
```
