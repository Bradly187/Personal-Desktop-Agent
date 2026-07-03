# Personal Desktop Agent

@AGENTS.md

<!-- ^ Shared cross-tool behavior rules (also read natively by Antigravity).
     Keep behavioral rules in AGENTS.md, not here, so both IDEs stay in sync. -->

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, hand gesture, iPad tilt, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). (Eye-gaze and head-pose control were removed — the standard iPad lacks the required TrueDepth sensor.) Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (15): `specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `specs/steering/tech.md`
- Open tasks: `specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/daily/`

## Current Status (2026-06-28)

> **Schema:** `agent.db` at `PRAGMA user_version = 9`. Do not rely on table counts in this file — `storage/db.py` is the authoritative source per AGENTS.md #1.

Phases 1–6 + Sprints A–C / 5–7 / G1–G5 / N–Q + cloud plan routing (PR #150) + chat attachments (PR #149) shipped and merged. Full dated history → [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Day-by-day notes → `docs/daily/`.

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

# Electron desktop shell — chat + dashboard iframes, file tree, Monaco editor,
# pty terminal. Attaches to a running backend, else spawns
# `main.py --chat --chat-no-browser` and owns its lifecycle.
# Spec: specs/desktop-app-shell/
cd desktop_app && npm install && npm start

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

- Architectural decisions + rejected alternatives: [docs/decisions.md](docs/decisions.md)

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

**Default runtime backend: Kokoro** (local ONNX, `tts_backend: "kokoro"` in `approval_config.json`, voice `af_bella`, CPU). Switch backends via `tts_backend` (`kokoro` | `polly` | `sapi`). **Note:** `approval_hook.py` speaks via Amazon Polly directly (hardcoded `_polly_speak`), independent of `tts_backend`.

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

## Feature Flags (DA_* environment variables)

Set `=0` / `=false` for byte-identical legacy behavior unless noted otherwise.
This table lists the headline flags; the authoritative registry of **all** DA_*
flags (incl. tuning knobs) is `core/flags.py`, validated at startup and enforced
by `tests/test_flags_registry.py`.

| Flag | Default | Summary | Decision | Spec |
|------|---------|---------|----------|------|
| `DA_PLAN_ASSUMPTIONS` | OFF | Surface assumptions in planner prompt and persist with plan | D024 | `specs/gap-1-assumptions.md` |
| `DA_REPLAN_CRITIC` | OFF | Run bounded critic-style check over recovery plans | D025 | `specs/gap-2-replan-critic.md` |
| `DA_WORKFLOW_VERIFY_CLOUD` | OFF | Route workflow verify judge through cloud | D026 | `specs/gap-3-verify-cloud.md` |
| `DA_RESUME_STALENESS` | OFF | Staleness check on resume seed and replayed reads | D027 | `specs/gap-4-resume-staleness.md` |
| `DA_CLOUD_PLAN` | OFF | Route `domain="plan"` to Bedrock Sonnet; avoids 18 GB model eviction | D015 | `specs/cloud-plan-routing/` |
| `DA_AUTO_ADJUDICATE` | OFF | Auto-dismiss hallucinated escalations using local model | D016 | `specs/deny-only-local-adjudicator/` |
| `DA_POST_RUN_WALKTHROUGH` | OFF | Generate walkthrough and TTS summary on success | D017 | `specs/post-run-walkthrough/` |
| `DA_TRAJECTORY_REDUCE` | ON | Compact trajectory tokens; flipped to ON despite ~12.5pt ordering regression | D011 | `specs/trajectory-reduction/` |
| `DA_CRITIC` | ON | Review diffs pre-disk-commit; REVISE drives replan | D007 | `specs/dev-agent-critic/` |
| `DA_TESTER` | ON | Auto-pytest after `.py` writes; failure = safe-observation, never rollback | D008 | `specs/dev-agent-critic/` |
| `DA_PLAN_PREVIEW` | OFF | Voice preview intent for large plans (threshold defaults to 3) | D018 | `specs/plan-preview-voice-gate/` |
| `DA_PLAN_REPAIR` | ON | Re-prompt planner on unknown-verb / unparseable plan (max `DA_PLAN_REPAIR_MAX=1`) | — | `specs/dev-agent-plan-contract/` |
| `DA_REPO_CONTEXT` | OFF | Inject stable repo facts (AGENTS.md/CLAUDE.md, layout, git) ahead of RAG | — | `specs/repo-context-ingestion/` |
| `DA_TRAJECTORY_DEDUP` | ON | Drop superseded duplicate reads from trajectory | — | `specs/trajectory-read-dedup/` |
| `DA_RESUME_MEMORY` | ON | Seed crash-resumed plans from prior run's `agent_steps` | — | `specs/resume-working-memory/` |
| `DA_SESSION_MEMORY` | OFF | Cross-session seed from related prior runs (Jaccard); precondition unmet — see D014 | D014 | `specs/resume-working-memory/` |
| `DA_DELEGATE` | OFF | Planner `[DELEGATE q]`: bounded read-only sub-agent investigation | — | `specs/dev-agent-delegate-verb/` |
| `DA_SAGA_ANNOUNCE` | ON | Speak TTS summary after saga rollback | — | `specs/dev-agent-sagas/` |
| `DA_SAGA_GIT_BACKEND` | OFF | git-blob snapshots instead of file-copy (no 256 KB cap) | D009 | `specs/dev-agent-sagas/` |
| `DA_DOMAIN_LEARN` | OFF | Dynamic domain-keyword overlay learning via `ContinuousTrainer` | — | — |

## Known Gotchas

- **The chat UI (`:8770`) requires an access token on every route except `/health`.** Token lives at `~/.claude/chat_server/token` (separate from the iPad pairing token — D020); present it via `X-Agent-Token`, `?token=`, or the session cookie set on the first tokened request. main.py opens the browser with the tokened URL; a 401 means reopen the URL logged at startup.

- **Voice approval gate requires an explicit confirmation word.** `WhisperStream._handle_approval_gate()` writes a response ONLY when `core/approval_keywords.classify_confirmation` detects a deliberate approve/deny. Ambient audio returns `None` → gate keeps waiting. Deny wins ties; utterances longer than `MAX_ANSWER_WORDS` (6) are treated as ambient. TTS echo suppressed 1.0s to prevent self-approval. Timeout/ambiguity/silence **fail safe to DENY**.

- **`pyautogui.typewrite` is ASCII-only.** `TYPE` keeps this limitation for backward compat. `DICTATE` uses `keyboard_paste()` (win32clipboard + Ctrl+V) and supports full unicode.

- **`WRITE_FILE` and `EDIT_FILE` are fail-closed.** Every write routes through `inference/edit_format.py` before touching disk; broken result → `EditError` (file untouched, feeds replan). `EDIT_FILE` SEARCH must match EXACTLY ONCE — stale/ambiguous → `EditError` (D013). Format per model via `edit_format_aci.per_model`; default `whole_file` (D006). Spec: `specs/edit-format-aci/`.

- **DevAgent saga: per-step compensation, not whole-tree git stash (D009).** `_snapshot_for_write` backs up individual files pre-write; `_halt_and_compensate` unwinds in reverse. Successful runs persist checkpoints which can be rolled back via VoiceRewindHandler ("undo that run"). Two preserved non-goals: Critic REVISE doesn't snapshot (pre-disk, D007); Tester failure never rolls back a good write (D008). Spec: `specs/dev-agent-sagas/`.

- **WSL terminal routing is ON by default.** Without it, bwrap/firejail never applies on a Windows host — `RUN_TERMINAL` falls through to allowlist-only silently. Windows-only commands (PowerShell/cmd/`*.exe`) stay native; `enabled: false` to opt out. Spec: `specs/wsl-terminal-routing/`.

- **Conversation mode uses anchored equality for wake/sleep detection (D012).** Fuzzy match rejected — "how do you say goodbye in French" must not exit sleep mode. `_speak_and_suppress` mutes mic before + after TTS to prevent self-transcription. Spec: `specs/conversation-mode/`.

- **Self-skilling rung 4 (autonomous authoring) is explicitly forbidden (D010).** Voice phrase `"save that as a command called X"` is the only macro promotion path. Nothing executes on silence. Spec: `specs/self-skilling/`.

- **Multi-agent workflow orchestration is OFF by default.** Voice triggers: `"think hard about …"` / `"research …"` / `"brainstorm …"`. Pure inference — no desktop/file/shell actions. `pipeline` mode is shipped and fully functional. Spec: `specs/workflow-orchestration/`.

- **VRAM model roster (RTX 5090; source of truth: `inference/model_router.py`):** baseline 8.3 GB + Whisper 4.2 GB → ~19 GB free for LLM. Command domain: `llama3.1:8b` (4.6 GB). Specialists: `qwen3-coder:30b` code+plan (thinking ON), `deepseek-r1:8b` math, `qwen3-vl:30b` vision, `gemma4:12b` general (D003; co-resides with command+Whisper). Flare fallback: `gemma4:e4b-it-qat`. `llama3.1:70b` does not fit alongside Whisper.

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
