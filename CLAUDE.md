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

## Current Status (2026-06-22)

> **Schema fact (authoritative):** `agent.db` = **42 tables** at `PRAGMA user_version = 8` (`storage/db.py` is the schema source of truth; v8 added the `commands.resolved_by` CLICK-resolver-tier column); `AnalyticsDB` (DuckDB) holds the **3** `benchmark_*` tables. Table counts in `docs/CHANGELOG.md` are historical (as-of-their-date), not current.

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

**Dev-agent verbs (5)** — emitted by specialist models via DevAgent:
`WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

The `CommandExecutor` handles all 16 verbs. The `DomainClassifier` determines which pipeline a query enters — accessibility (llama3.1:8b, verb-first) or dev-agent (specialist model, free-form).

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

Current voice: **Danielle** (en-US, Generative engine, 24 kHz). Change via `"voice_id"` in `approval_config.json` — takes effect immediately, no restart needed.

## WebSocket Protocol

> Full message-type reference: [docs/websocket-protocol.md](docs/websocket-protocol.md)

iPad → PC (25 types): `tilt` `tilt_position` `tilt_tap` `tilt_ratchet` `keyword` `audio_stream` `camera_frame` `depth_frame` `touch_command` `trackpad` `handwriting_image` `dwell_click` `ping` `set_dwell_action` `set_feature_toggle` `sensor_switch` `cursor_pause` `cursor_resume` `gesture_assessment` `pain_day_override` `flare_profile` `calibration_start` `calibration_cancel` `mic_mute` `ipad_log`

PC → iPad (6 types): `ack` `status` `screenshot` `handwriting_result` `recalibration_request` `mic_state`

## Coding Conventions

- All pipeline classes are `async`; blocking I/O uses `asyncio.to_thread`
- Every sensor class must degrade gracefully — wrap hardware imports in `try/except ImportError`, log a warning, never crash
- No global state outside dataclass instances; all state lives in class attributes
- `Command` is the universal DTO — never pass raw dicts across pipeline boundaries
- Log levels: DEBUG per-frame, INFO commands/routing, WARNING sensor failures, ERROR unrecoverable

## Known Gotchas

- **Voice approval gate requires an explicit confirmation word.** While `approval_hook.py`'s `~/.claude/approval/pending` file exists, `WhisperStream._handle_approval_gate()` writes a response ONLY when the transcript classifies as a deliberate approve/deny (`core/approval_keywords.classify_confirmation` — single source of truth shared with `approval_hook.py`). Ambient audio / podcast speech / a stray word returns `None` → discarded, gate keeps waiting. Deny wins ties; utterances longer than `MAX_ANSWER_WORDS` (6) are treated as ambient. The TTS echo is suppressed for 1.0s so Danielle's spoken "Approve …?" can't self-approve. Timeout/ambiguity/silence **fail safe to DENY**. Tests: `tests/test_approval_gate.py` (44).

- **Domain-classifier learning is experimental and OFF by default (`DA_DOMAIN_LEARN`).** With the flag unset, `DomainClassifier` is the static-keyword classifier and the `router_domains` eval baseline holds. When on, `ContinuousTrainer._learn_domain_overlay` learns per-domain vocabulary into `domain_keyword_weights` (bounded nudge, capped at `_MAX_OVERLAY_NUDGE=15`, never overrides static scores). Rollback: misroute rate rise clears that domain's overlay. Tests: `test_domain_overlay.py` (7), `test_domain_misroutes.py` (3).

- **VRAM model roster (RTX 5090, measured 2026-05-08):** baseline 8.3 GB, Whisper +4.2 GB, ~19 GB free for LLM. Default command domain: `llama3.1:8b` (4.6 GB). Specialist models: `qwen3-coder:30b` (code+plan, thinking ON), `deepseek-r1:8b` (math, chain-of-thought kept), `qwen3-vl:30b` (vision), `gemma3:27b` (general). `llama3.1:70b` does not fit alongside Whisper. `nemotron-mini` (25%) and `gpt-oss:20b` (0%) were removed. `deepseek-r1:8b` reasoning output is kept for math but is incompatible with verb-first command format.

- **DevAgent trajectory reduction is experimental and OFF by default (`DA_TRAJECTORY_REDUCE`).** With the flag unset, `_replan`/`_try_replan`/`_reflect` render the executed-step trajectory exactly as before (byte-identical legacy path). When on, `inference/trajectory.render_trajectory` synthesizes the re-sent trajectory — keeps the most recent 3 steps verbatim, abstracts older successes, collapses older read-only runs, and ALWAYS preserves failure signal — to cut prompt tokens on replan/reflect (lower local latency + Bedrock spend on escalation). Deterministic, no LLM call. Spec: `specs/trajectory-reduction/`. Tests: `test_trajectory_reduction.py` (11). Flip default on only after the replan eval baseline locks.

- **WRITE_FILE is lint-gated, and its edit format is a per-model knob (default `whole_file`).** Every `WRITE_FILE` now routes through `inference/edit_format.py` `EditApplier.apply(current, body, edit_format, path)` *before* the write touches disk: it builds the resulting text for the format, then runs the registered validator (`.py` → `ast.parse`). A syntactically-broken result raises `EditError` (fail-closed — file untouched, no saga compensation registered) and the diagnostic becomes the step result the replan loop reacts to. The format is resolved per plan-model via `ModelRouter.edit_format_for()` (config `edit_format_aci.per_model` in `~/.claude/ipad_bridge/config.json` → `ModelProfile.edit_format` → `whole_file`); `DevAgent._active_plan_model` records which model planned so `_apply_edit` applies its format. **Default is `whole_file` everywhere — byte-identical to legacy except broken Python is now rejected pre-write.** `hashline` (line:hash-anchored ops, `@@ REPLACE|DELETE|INSERT_*`) is implemented and activates ONLY for a model configured for it (then `READ_FILE` renders `lineno:hash|content` anchors and the plan prompt gets `HASHLINE_PROMPT_INSTRUCTIONS`); `udiff` is reserved (degrades to whole_file). **Gate verdict (task 6 A/B, `evals/ --mode edit_ab`, qwen3-coder:30b): default stays `whole_file`** — silent elision (whole_file's theorized weakness) did not occur even on ~180-line files, whole_file led on correctness (100% vs 80% on the hard subset), and hashline's gain is purely efficiency (~9–23× less output, ~2–4× faster), so it's an opt-in cost play, not a correctness upgrade. Spec: `specs/edit-format-aci/`. Tests: `test_edit_format.py` (32) + `test_evals_edit_ab.py` (11); baselines `evals/baselines/edit_format*.json`.

- **Plan-parse auto-repair is experimental and OFF by default (`DA_PLAN_REPAIR`).** With the flag unset, a planner step with an unknown verb is silently dropped and a fully-unparseable plan wraps as one EXPLAIN (legacy, byte-identical). When on, `DevAgent._acquire_plan_steps` re-prompts the planner with a corrective message (`_build_plan_repair_prompt`, naming the dropped verb/failure + restating the schema) up to `DA_PLAN_REPAIR_MAX` (default 1) times before failing safe — never executes a partial/guessed plan. `_parse_plan_json_report` records drops (`PlanParseReport`/`DroppedStep`) instead of swallowing them; `_parse_plan_json` is a back-compat raising wrapper. Backend-agnostic, so a future cloud-executable plan is covered too (R2 deferred — the cloud plan path is advisory-only, `steps: 0`, never parsed). Spec: `specs/dev-agent-plan-contract/`. Tests: `test_plan_contract.py` (13) + model-free eval `evals/plan_contract.py` (`--check` gate, baseline locked `exact_acc=1.0`) + `test_evals_plan_contract.py` (6). Flip default on only after the eval baseline holds in production.

- **Independent Critic + autonomous Tester on WRITE_FILE are experimental and OFF by default (`DA_CRITIC`, `DA_TESTER`).** With both unset the WRITE_FILE path is byte-identical legacy (confirm→apply→snapshot→write). **Critic** (on): after the lint gate passes, `inference/critic.py` reviews the resulting diff on the already-loaded plan/general model with a fresh reviewer context (no generator CoT, no new VRAM — AGENTS.md #6) and returns a `CriticVerdict`. PASS commits — a low-confidence PASS (`<DA_CRITIC_FLOOR`, default 0.6) forces an explicit confirm via `_confirm_destructive_op(force=True)`, which only ever ADDS friction (never weakens a gate); REVISE/BLOCK blocks the write with a diagnostic that drives `_replan` (no snapshot/compensation), bounded by `DA_CRITIC_MAX_REVISIONS` (default 1) per path. `parse_verdict` is conservative (unparseable→REVISE; a security/correctness finding floors PASS→REVISE); a Critic error fails safe to escalate-confirm, never silent auto-approve. Generalizes the `_verify_math_with_cas` precedent from math to code. **Tester** (on): after a committed `.py` SOURCE write, `inference/tester.py` generates a focused pytest test and runs it one-shot through `inference/sandbox.run_sandboxed`; the outcome is appended to the step result as an **observation** (safe-observation — a failing generated test is surfaced for `_reflect`/replan but the good write is NEVER rolled back). Degrades gracefully, skip-on-flare hook, never reports a skip as a pass. Spec: `specs/dev-agent-critic/`. Tests: `test_critic.py` (16) + `test_tester.py` (17). Default flip awaits the model-driven `dev_critic` eval (task 6).

- **WSL terminal routing is ON by default (`wsl_terminal_routing.enabled`, default `true` since 2026-06-21).** On a Windows host the bwrap/firejail jail (`inference/sandbox.py`) can't apply natively — `sandbox_tool()` returns None and RUN_TERMINAL would fall through to the unsandboxed (allowlist-only) path. WSL routing closes that gap: `_maybe_run_wsl` runs a **WSL-safe** command inside WSL2 via `wsl.exe -e <bwrap-jail>` so the namespace jail actually applies on the host. It's scope-preserving: `_path_in_scope` runs upstream on the Windows path, and `to_wsl_path` is a 1:1 `E:\…`→`/mnt/e/…` map that **refuses** UNC/non-drive paths (→ native, never a guess). The compatibility boundary (`command_is_wsl_safe`) keeps Windows-only commands native — PowerShell/`cmd`/`*.exe`/drive-anchored/unknown-exe (under default `unknown_command_policy=native`) all fall back. Decision order is WSL → native, degrading gracefully (WSL absent / no in-distro bwrap / untranslatable → native, logged). Set `enabled: false` in the config block to opt out. Smoke gate: `tests/smoke_wsl_gate.py`; audit: `docs/audits/2026-06-21-wsl-smoke-gate.md`. Spec: `specs/wsl-terminal-routing/`. Tests: `test_wsl_routing.py` (36).

- **`pyautogui.typewrite` is ASCII-only** — `TYPE` keeps this limitation for backward compat. `DICTATE` uses `keyboard_paste()` (win32clipboard + Ctrl+V) and supports full unicode.

- **Self-skilling (macros) is experimental and OFF by default (`self_skilling.enabled` in `~/.claude/ipad_bridge/config.json`).** This is rung 2 of the self-skilling ladder (specs/self-skilling/): the agent learns *new capabilities from its own experience* by crystallizing recurring multi-step plans into named, replayable macros. With the flag unset, nothing runs — byte-identical legacy. When on, `adaptive/macro_detector.py` `MacroDetector` mines successful trajectories (`agent_runs` ⨝ `agent_steps`) **offline** in a supervised loop (never the 60 Hz path, AGENTS.md #2; skipped during a flare, AGENTS.md #5) — deterministic, NO LLM: it reduces each run to a *plan signature* (verb sequence with literal args abstracted to typed slots), clusters by signature, and stages a `kind="macro"` candidate in `self_evolution_candidates` (no schema change — reuses the staging table). Detection only **announces** ("say: save that as a command called …"); the voice phrase `"save that as a command called X"` (`core/macro_store.parse_macro_save`) is the **only** path that promotes — fail-safe-DENY, nothing is enabled on silence (AGENTS.md #4). `core/macro_store.py` `MacroStore` registers a promoted macro as a routable intent and replays it through the normal `CommandExecutor` (every gate still fires); replay verifies **all** constituent tools exist before executing **any** step and fails safe with CLARIFY otherwise (R5.3 — never a partial macro). Single-value arg slots become baked defaults, multi-value slots become required parameters. **Rung 3 (gap-driven skill *drafting*, human-gated) and rung 4 (autonomous authoring) are explicit non-goals of this sprint** — rung 4 is forbidden by the spec. Gate before the default flip: the rung-2 baseline holding in production. Spec: `specs/self-skilling/`. Tests: `test_self_skilling.py` (25) + model-free eval `evals/self_skilling.py` (`--check` gate, baseline locked `exact_acc=1.0`) + `test_evals_self_skilling.py` (5).

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
