# Decision Log

Append-only. Only log decisions with a meaningful rejected alternative — minor
implementation choices don't belong here. Keep the index under 30 lines.

See AGENTS.md Rule 12 for when and how to add entries.

---

## Index (newest first)

- [D031 — 2026-08-16 — Scientific keypad struck; Pencil canvas covers the job at lower joint cost](#d031)
- [D030 — 2026-08-16 — iPad camera/LiDAR producers struck; L515 keeps the same message types](#d030)
- [D029 — 2026-07-05 — Full pytest suite gates in CI on windows-latest; ruff gate with parked style ignores](#d029)
- [D028 — 2026-07-03 — Retrieval quality measured natively in evals/, not via pgvector migration + deepeval](#d028)
- [D027 — 2026-07-03 — Staleness check on resume seed and replayed reads](#d027)
- [D026 — 2026-07-03 — Cross-model verify judge for workflow fan-out](#d026)
- [D025 — 2026-07-03 — Independent review of recovery plans (replan critic)](#d025)
- [D024 — 2026-07-03 — Assumption surfacing in the planner prompt](#d024)
- [D023 — 2026-07-02 — Chat transcript markdown renders via vendored marked+DOMPurify, not CDN or hand-rolled](#d023)
- [D022 — 2026-07-01 — VoiceSystemControl keeps condition/calibration switching on HybridCoordinator, not moved](#d022)
- [D021 — 2026-07-01 — Flag registry is a passive validation mirror, not a config read-through](#d021)
- [D020 — 2026-07-01 — Chat server gets its own token (cookie-delivered), not the iPad pairing token](#d020)
- [D019 — 2026-07-01 — Chatterbox TTS backend removed; Kokoro covers the use case](#d019)
- [D018 — 2026-06-29 — Trajectory evals constrain the plan path with production's grammar; baselines re-locked, planner-prompt fix deferred](#d018)
- [D017 — 2026-06-28 — Pre-commit hook is the mechanical doc-drift enforcement boundary](#d017)
- [D016 — 2026-06-28 — /doc-update is a slash command, not a Stop hook](#d016)
- [D015 — 2026-06-28 — DA_CLOUD_PLAN routes plan domain only, not full DevAgent](#d015)
- [D014 — 2026-06-28 — DA_SESSION_MEMORY held OFF until relevance validated](#d014)
- [D013 — 2026-06-25 — EDIT_FILE SEARCH block is fail-closed (exact match only)](#d013)
- [D012 — 2026-06-25 — Conversation mode wake/sleep uses anchored equality, not fuzzy match](#d012)
- [D011 — 2026-06-24 — DA_TRAJECTORY_REDUCE held OFF despite passing gate](#d011)
- [D010 — 2026-06-22 — Self-skilling rung 4 (autonomous authoring) explicitly forbidden](#d010)
- [D009 — 2026-06-27 — DevAgent saga uses per-step compensation, not whole-tree git stash](#d009)
- [D008 — 2026-06-21 — Tester failure is a safe-observation; it never rolls back a good write](#d008)
- [D007 — 2026-06-21 — Critic REVISE runs pre-disk-commit; no snapshot before Critic](#d007)
- [D006 — 2026-06-20 — Whole-file edit format is the default (not hashline or udiff)](#d006)
- [D005 — 2026-06-21 — WSL terminal routing is ON by default](#d005)
- [D004 — 2026-06-23 — L515 head-pointer: depth_comp=0 (depth compensation disabled)](#d004)
- [D003 — 2026-06-07 — gemma4:12b fills the general-domain slot (gemma3:27b retired)](#d003)
- [D002 — 2026-06-19 — Cloud backend is Amazon Bedrock only; direct Anthropic API removed](#d002)

*Index holds the 30 most recent. Older entries remain in full in the body below —
D001 (MOUSEDOWN/MOUSEUP synchronous, no `asyncio.to_thread`) is still live and is
referenced from CLAUDE.md §Action Vocabulary.*

---

## Entries

---

### D031 — Scientific keypad struck; Pencil canvas covers the job at lower joint cost {#d031}
**Date:** 2026-08-16
**Chose:** Strike Requirement 18 (`ScientificKeypadView`). The Pencil handwriting canvas
(Requirement 19) is the sole math-entry surface.
**Rejected:** Build the keypad as specced in `specs/ipad-sensor-focus/scientific-keypad.md`.
**Why:** Both surfaces terminate in the identical `touch_command`/`DICTATE` clipboard-paste
delivery, so they compete for exactly one job. For an RA user the cost that matters is joint
actuations: `sin(π/4) + √2` is ~14 discrete taps on a keypad versus one continuous Pencil
stroke sequence on the canvas, which is already shipped and meets R19 in full. A second, more
painful path to an already-solved outcome is negative value, not optional value. Note this
strike also corrects a documentation-integrity failure — `tasks.md` 2.14 was marked `[x]` for
a `ScientificKeypadView.swift` that was never written, and `OverlayPreservationTests.swift`
carried a `testScientificKeypadSendExpression` that only exercised the shared DICTATE send
path. Both fixed in the same change. The design doc is retained as a record.
**Ref:** `specs/ipad-sensor-focus/requirements.md` R18, `specs/ipad-sensor-focus/scientific-keypad.md`, `docs/audits/2026-08-13-ipad-swift-accessibility-gap-analysis.md`

---

### D030 — iPad camera/LiDAR producers struck; L515 keeps the same message types {#d030}
**Date:** 2026-08-16
**Chose:** Strike Requirements 7 and 10 (iPad LiDAR depth, iPad camera gesture) — the iPad is
not a vision producer. **Keep** `sensors/gesture_processor.py`, `sensors/lidar_receiver.py`,
and the `camera_frame`/`depth_frame` handlers in `core/ipad_bridge.py` untouched.
**Rejected (primary):** Build the iPad-side capture (`LiDARStreamer.swift` + camera frames).
**Rejected (secondary):** Delete the PC-side receivers along with the requirements, on the
belief that they were orphaned.
**Why:** *The device has no LiDAR scanner and the spec's "iPad Pro (2020+)" premise is false* —
the unit in use is a standard iPad, the same root cause that killed R3/R4 (no TrueDepth). These
producers were not merely never built: `LiDARStreamer.swift` (370 lines, emitting **both**
`depth_frame` at 5 fps and `camera_frame` at 10 fps) shipped 2026-05-16 and was stripped
2026-05-24 in commit `64eec10` because it was unused *and* because on iOS 26
`ARWorldTrackingConfiguration.supportsFrameSemantics` crashed the Settings tab on every render
on non-LiDAR hardware. This decision ratifies that removal at the requirements level rather than
leaving R7/R10 reading as pending work. Rebuilding a standalone camera path was separately
rejected on ergonomics: the iPad is the touch surface, so the hand on the screen is the hand the
camera would be tracking. The secondary rejection is the load-bearing part: the receivers look
orphaned but are **live**. `sensors/realsense_publisher.py` connects
to the same bridge as a WebSocket client and emits the **same** `camera_frame` and
`depth_frame` message types from the RealSense L515, which is the working gesture/hand-pointer
camera (see D004). `main.py:1036-1037` wires both receivers unconditionally, and
`tests/test_gesture_*.py`, `test_lidar_receiver.py` cover them. Deleting them to "clean up
after" this strike would have broken the L515 path and the D7 flick-to-snap gestures. The
message types stay in the protocol; only their producer changes identity.
**Ref:** `specs/ipad-sensor-focus/requirements.md` R7/R10, `sensors/realsense_publisher.py`, `docs/websocket-protocol.md`, `docs/audits/2026-08-13-ipad-swift-accessibility-gap-analysis.md`

---

### D029 — Full pytest suite gates in CI on windows-latest; ruff gate with parked style ignores {#d029}
**Date:** 2026-07-05
**Chose:** New `tests.yml` workflow: the full unit suite (~2,750 tests) runs on `windows-latest` with the pinned `requirements.txt`, plus a `ruff check` job on ubuntu. Ruff starts with pyflakes + pycodestyle-error defaults and parks five style rules (E402/E702/E731/E741/F841) as documented ratchet ignores in `pyproject.toml`; availability-probe imports keep `# noqa: F401` (the import IS the probe — `find_spec` would not exercise side effects like `comtypes.gen` codegen).
**Rejected:** (a) ubuntu runner with a curated dependency subset — Windows is the only ship target (pywin32, windows-curses, Win32 UIAutomation); a Linux run would validate a platform we don't ship and need a second requirements file that drifts; (b) adopting `ruff format`/black in the same change — a whole-repo reformat buries the 12 real F821/F811 findings in noise; (c) fixing all 446 lint findings at once — the parked rules are style-only debt, tightened one rule at a time.
**Ref:** `.github/workflows/tests.yml`, `pyproject.toml` [tool.ruff], `docs/audits/2026-07-05-industry-patterns-gap-analysis.md` (IG-1/IG-2)

---

### D028 — Retrieval quality measured natively in evals/, not via pgvector migration + deepeval {#d028}
**Date:** 2026-07-03
**Chose:** A retrieval-rank eval mode (`--mode retrieval`, MRR gated + Hit@5) in the existing `evals/` harness, with ground truth generated locally (`scripts/generate_retrieval_eval_data.py`, Ollama `llama3.1:8b`, self-retrieval filter). Targets match on `(file, name)` hit metadata, never chroma ids (ids hash mtime and churn on reindex).
**Rejected:** (a) Migrating vector memory to PostgreSQL+pgvector via Docker — `docs/architecture/database-design.md` §1 already rejects a DB server as overkill for a single-user local tool, and ChromaDB provides the same vector+metadata hybrid queries; (b) SQS/Lambda queue-driven ingestion — violates the single-machine local-first posture (D015; `specs/behavioral-twin-state` no-cloud requirement) with no throughput problem to solve; (c) a deepeval/pytest parallel harness — AGENTS.md Rule 9 routes behavioral verification into `evals/`, and MRR/Hit@K are ~20 lines of arithmetic; (d) NDCG — with one relevant chunk per case it is a monotone transform of MRR.
**Ref:** `specs/retrieval-quality-eval/`, `evals/retrieval.py`, `scripts/generate_retrieval_eval_data.py`

---

### D027 — Staleness check on resume seed and replayed reads {#d027}
**Date:** 2026-07-03
**Chose:** At resume, stat each path in WorkingMemory.files; annotate entries changed since the step timestamp as stale, and drop stale notes derived from those files. Do not re-execute reads — just label, and let the planner decide to re-read.
**Rejected:** Silently resuming with potentially outdated files, or re-executing all reads automatically.
**Why:** Files can be modified between a crash and resume. Automatic re-execution is unnecessary and can be expensive. Flagging them as stale empowers the planner to decide if it needs fresh context.
**Ref:** `inference/working_memory.py`, `inference/dev_agent.py`

---

### D026 — Cross-model verify judge for workflow fan-out {#d026}
**Date:** 2026-07-03
**Chose:** Route the verify judge through the Bedrock cloud backend when available (`DA_WORKFLOW_VERIFY_CLOUD`), falling back to local judge if cloud is down/disabled.
**Rejected:** Use multiple models voting (voting judge) or run entirely locally.
**Why:** Voting was considered and is not worth the token cost at this scale; a different-model judge captures most of the benefit of independent weights to avoid correlated reviewer blind spots.
**Ref:** `inference/workflow.py`, `core/cloud_backend.py`

---

### D025 — Independent review of recovery plans (replan critic) {#d025}
**Date:** 2026-07-03
**Chose:** Run a bounded critic-style check over a new recovery plan after parsing (`DA_REPLAN_CRITIC`). REVISE consumes the existing replan budget.
**Rejected:** Let recovery plans run unreviewed (since they are generated after something went wrong), or add unbounded loop.
**Why:** Closes the only unreviewed self-grading loop. Recovery plans are generated precisely when context is most likely poisoned.
**Ref:** `inference/dev_agent.py`

---

### D024 — Assumption surfacing in the planner prompt {#d024}
**Date:** 2026-07-03
**Chose:** Add an optional `assumptions` array to `_PLAN_JSON_SCHEMA` and instruct planner to list assumptions about repo/system state (`DA_PLAN_ASSUMPTIONS`).
**Rejected:** Leave assumptions implicit.
**Why:** A wrong premise enters the trajectory silently and conditions every later step. Surfacing them allows for observability and debugging, especially when plans fail.
**Ref:** `inference/model_router.py`, `inference/dev_agent.py`

---

### D023 — Chat transcript markdown renders via vendored marked+DOMPurify, not CDN or hand-rolled {#d023}
**Date:** 2026-07-02
**Chose:** The chat UI's markdown pipeline is two vendored static assets (`web_client_chat/vendor/marked.min.js` 12.0.2 + `purify.min.js` DOMPurify 3.1.6, ~57 KB total); every render passes through `DOMPurify.sanitize` and falls back to plain `textContent` when either is unavailable. Enforced by `tests/test_chat_assets.py`.
**Rejected:** (a) CDN `import` like the mermaid DAG module — the transcript is core UX and must work offline, whereas the DAG pane is an optional enhancement; (b) a hand-rolled markdown subset — a correctness/XSS maintenance sink for zero dependency savings once sanitization is required anyway (LLM output is untrusted input).
**Why:** Offline-safe core path with a sanitizer that has real security review; the fallback keeps the old plain-text behavior as the degraded mode.
**Ref:** `specs/chat-workbench-parity/` R2, `web_client_chat/vendor/`, `tests/test_chat_assets.py`

---

### D022 — VoiceSystemControl keeps condition/calibration switching on HybridCoordinator, not moved {#d022}
**Date:** 2026-07-01
**Chose:** During the HybridCoordinator decomposition (`specs/hybrid-coordinator-decomposition/`), `_switch_condition`/`_run_calibration` stay defined on `HybridCoordinator` and are passed into `VoiceSystemControl` as narrow async delegate callables (`switch_condition`, `run_calibration`), rather than moving the method bodies into `VoiceSystemControl` itself.
**Rejected:** Move both methods' full bodies into `VoiceSystemControl` alongside the rest of the voice-keyword block they're triggered from.
**Why:** `_switch_condition` calls `self._profiler.apply_to(self._whisper, coordinator=self)` — an external `ConditionProfiler` API that takes the *real* `HybridCoordinator` instance as a `coordinator=` kwarg. Moving the method into `VoiceSystemControl` would force it to hold a back-reference to the full coordinator, violating the module's explicit-DI requirement (spec R3 — no back-reference, only named accessor callables). Leaving the method on the coordinator and injecting it as a delegate satisfies both the external API contract and the DI requirement; the fallback matches spec R4.3 ("if a branch can't be cleanly attributed, leave it in HybridCoordinator").
**Ref:** `core/hybrid_coordinator.py` (`_switch_condition`, `_run_calibration`), `core/voice_system_control.py`

---

### D021 — Flag registry is a passive validation mirror, not a config read-through {#d021}
**Date:** 2026-07-01
**Chose:** `core/flags.py` declares every DA_* flag (name/kind/default); main.py validates the environment once at startup (WARN on unknown or unparseable, INFO the active set). Call sites keep reading `os.environ` exactly as before; `tests/test_flags_registry.py` sweeps the source tree to keep registry and code in lockstep both directions.
**Rejected:** Refactor all 25+ call sites to read through a central config object.
**Why:** The failure mode being fixed is *silent* misconfiguration (typo'd name, malformed value), which validation alone eliminates. A read-through refactor would touch every module (large conflict surface against the open PR #153 decomposition), change flag-read timing semantics (several sites deliberately re-read per call), and risk behavior drift for zero additional safety. The sweep test gives the lockstep guarantee a read-through would have provided structurally.
**Ref:** `core/flags.py`, `tests/test_flags_registry.py`

---

### D020 — Chat server gets its own token (cookie-delivered), not the iPad pairing token {#d020}
**Date:** 2026-07-01
**Chose:** Separate per-install token at `~/.claude/chat_server/token`, required on every route except `/health` via aiohttp middleware (header / `?token=` query / HttpOnly cookie, constant-time compare). main.py opens the UI at `/?token=…`; the response cookie authenticates the static assets, `/chat` WS handshake, and fetches with zero JS changes.
**Rejected (primary):** Reuse the iPad bridge pairing token. **Rejected (secondary):** Keep relying on the 127.0.0.1 default bind as the only protection.
**Why:** A shared token couples rotation — revoking a lost iPad would break the desktop chat UI and vice versa. Loopback-only binding is one constructor argument away from LAN exposure, and the chat socket reaches DevAgent with file-write and terminal rights plus an open `/upload`; the bridge (same trust boundary) was already token-gated, so the asymmetry was the anomaly. Cookie delivery (Jupyter pattern) was chosen over query-token-everywhere because the WS handshake and asset fetches inherit it for free.
**Ref:** `core/chat_server.py`, `tests/test_chat_server_auth.py`

---

### D019 — Chatterbox TTS backend removed; Kokoro covers the use case {#d019}
**Date:** 2026-07-01
**Chose:** Delete `tts/chatterbox_tts.py` and all config/requirement references.
**Rejected:** Keep Chatterbox as a documented optional path.
**Why:** Chatterbox was added before Kokoro existed. It hard-pins `torch==2.6.0`
(incompatible with the current `torch 2.12.0` stack), causing it to be moved to
"install-separately" in PR #125. `chatterbox_voice_ref` has been `null` since
initial config — the zero-shot voice-cloning feature was never exercised.
Kokoro (local ONNX, default since 2026-06-23) covers local/offline/zero-cost TTS
without the torch conflict, with GPU auto-selection on `onnxruntime-gpu`. Keeping
Chatterbox is dead code maintenance burden on a production accessibility dependency.
**Ref:** `specs/chatterbox-removal/`, PR #125 (original demotion to install-separately)

---

### D018 — Trajectory evals constrain the plan path with production's grammar; baselines re-locked, planner-prompt fix deferred {#d018}
**Date:** 2026-06-29
**Chose:** Give the `dev_trajectory` / `dev_critic` evals grammar parity with production — the plan predictor now passes production's `_PLAN_JSON_SCHEMA` as Ollama `format=` (imported, never copied). Re-lock both baselines under that constraint (`dev_critic` 1.0, `dev_trajectory` 0.6364, tol 0.1). **Defer** the planner-prompt / under-planning-repair change (spec R3 / Phase 4).
**Rejected (primary):** Tune `_PLAN_PROMPT` now to recover the old 0.7273 `dev_trajectory` number. **Rejected (secondary):** Re-record the baseline *unconstrained* (papering over the eval-vs-prod gap), or loosen the gate.
**Why:** The 2026-06-29 regression (`dev_trajectory` 0.7273→0.545, `dev_critic` 1.0→0.875) was **not** model drift (snapshot is 2026-03-08, older than the 2026-06-14 baseline) and **not** a suite edit. Two causes: (1) the eval scored the plan path *unconstrained* while production forces a JSON `steps` grammar — added after the baseline was locked, so the eval silently drifted; the unconstrained model reverted to legacy bracket notation and single-line plans the grammar would forbid. (2) `_PLAN_PROMPT` edits since 2026-06-14 (EDIT_FILE verb #134, mini-agent gaps A–D) made the one-shot planner more investigate-first. Closing the fidelity gap recovered `dev_critic` fully (→1.0) and `dev_trajectory` to a passing 0.6364. The residual `dev_trajectory` misses are genuine one-shot under-planning (plans that stop after `GIT_STATUS`/`READ_FILE` before the requested write/commit) — but production's DevAgent runs **iteratively** (plan→execute→observe→replan), so an investigate-first plan is plausibly correct there and the one-shot trajectory eval can't credit it. Building R3 against a one-shot proxy risks optimizing the wrong surface; the precondition for R3 is an **execution-mode** (iterative) measurement showing production is actually harmed. `safe_acc` stayed 1.0 throughout — never a safety regression.
**Ref:** `specs/dev-agent-plan-fidelity/`, `evals/runner.py::plan_predictor`, `evals/run.py::_plan_grammar`, `inference/local_inference.py::OllamaInference._chat`

---

### D017 — Pre-commit hook is the mechanical doc-drift enforcement boundary {#d017}
**Date:** 2026-06-28
**Chose:** Git pre-commit hook in `scripts/hooks/pre-commit` that fails the commit if CLAUDE.md Gotchas exceed 12 entries or `docs/decisions.md` index exceeds 30 lines.
**Rejected (primary):** GitHub Actions CI check on the same conditions. **Rejected (secondary):** Trust AGENTS.md Rule 13 alone with no mechanical enforcement.
**Why:** Pre-commit runs at the boundary where drift is cheapest to fix — before the commit exists, without CI round-trip latency. CI catches it after push, when the commit is already in history and reverting is more disruptive. Rule 13 alone has already proven insufficient in this codebase (the pruning pass that motivated this session). The pre-commit hook adds zero noise when the file is healthy and blocks exactly when discipline has slipped.
**Ref:** `scripts/hooks/pre-commit`, AGENTS.md Rule 13

---

### D016 — /doc-update is a slash command, not a Stop hook {#d016}
**Date:** 2026-06-28
**Chose:** Claude Code slash command at `.claude/commands/doc-update.md`, invoked deliberately by the agent when a shipping event occurs.
**Rejected:** Claude Code `Stop` hook that fires automatically at the end of every agent turn.
**Why:** Stop hooks fire on every turn — 20+ times per session, including conversational turns with no code changes. An auto-firing hygiene checklist on every response would be ignored immediately (training the agent to treat it as noise) or would slow every interaction. The slash command is invoked with intent: the agent knows when a feature shipped and runs the checklist then. Lower frequency, higher signal. If the agent forgets, the pre-commit hook (D017) provides the mechanical backstop.
**Ref:** `.claude/commands/doc-update.md`, AGENTS.md Rule 13

---

### D015 — DA_CLOUD_PLAN routes plan domain only, not full DevAgent {#d015}
**Date:** 2026-06-28  
**Chose:** Route only `domain="plan"` to Bedrock via `CloudPlanRouter`; all other domains and all execution stay local.  
**Rejected:** Route the full `DevAgent` (planning + step execution) cloud-side via `--cloud-dev-agent`.  
**Why:** Execution must stay local — desktop/file/shell actions via cloud would break the `writable_roots` sandbox (#7), add network latency to timing-critical verbs, and expose the raw action stream (not just goal+context scrubbed by `ContentFilter`). The VRAM problem was purely the 18 GB plan-model eviction cycle, not the execution path. Cloud DevAgent remains a separate, opt-in flag for non-execution queries.  
**Ref:** `specs/cloud-plan-routing/`, PR #150

---

### D014 — DA_SESSION_MEMORY held OFF until relevance validated {#d014}
**Date:** 2026-06-28  
**Chose:** Ship `DA_SESSION_MEMORY` feature flag default OFF.  
**Rejected:** Flip ON optimistically alongside the other mini-coding-agent gap flags.  
**Why:** Precondition unmet — live `agent.db` has 0 multi-step file-touch runs (only the demo agent is present). Jaccard relevance (`score_relevance`) dilutes file-path tokens, so cross-session seeding would inject noise rather than signal. Flipping ON without real runs would corrupt resumption context invisibly. Validation harness at `scripts/validate_session_memory.py`.  
**Ref:** `specs/resume-working-memory/` (R4), memory entry `specs-vs-code-gap-2026-06-28.md`

---

### D013 — EDIT_FILE SEARCH block is fail-closed (exact match only) {#d013}
**Date:** 2026-06-25  
**Chose:** SEARCH not found exactly once → `EditError`, file untouched. No fuzzy fallback.  
**Rejected:** Apply fuzzy/whitespace-normalized match when exact match fails, fall through to best-effort.  
**Why:** A stale or ambiguous SEARCH means the model's mental model of the file is wrong; applying a fuzzy guess would silently corrupt the file. The same fail-closed logic already governs `WRITE_FILE` (lint gate) and the approval gate (#4). A replan on `EditError` with an updated SEARCH is safer than a silent wrong edit. The `udiff` layered approach (exact → ws → fuzzy) applies only within a single SEARCH block's internal resolution, not across blocks.  
**Ref:** `specs/edit-format-aci/` R5 (tasks 8–9)

---

### D012 — Conversation mode wake/sleep uses anchored equality, not fuzzy match {#d012}
**Date:** 2026-06-25  
**Chose:** Detect wake/sleep phrases via normalize → drop filler → set membership (anchored equality).  
**Rejected:** Semantic/embedding similarity or fuzzy string match for phrase detection.  
**Why:** "How do you say goodbye in French?" must not trigger sleep mode; "Let's talk about that" must not trigger wake mode. With fuzzy matching, any utterance mentioning "talk" or "goodbye" could fire the transition. Anchored equality is deterministic, tick-safe (#2), and makes the guard testable without a live model. False-negative (user has to repeat the exact phrase) is acceptable; false-positive (mode flip on ambient speech) would silently break the pipeline.  
**Ref:** `specs/conversation-mode/`, `core/conversation_mode.py`

---

### D011 — DA_TRAJECTORY_REDUCE held OFF despite passing gate {#d011}
**Date:** 2026-06-24  
**Chose:** Keep `DA_TRAJECTORY_REDUCE` default OFF; document as deliberate trade-off hold.  
**Rejected:** Flip ON since the baseline passes its eval gate.  
**Why:** Baseline passes but carries a documented ~12.5pt recovery-ordering regression on long-prefix builds — the compactor abstracts older successful steps, and the planner occasionally mis-orders recovery actions when that context is missing. The token savings are real but the regression touches safety-critical replan behavior. Hold until a targeted eval for recovery ordering is locked.  
**Ref:** `specs/trajectory-reduction/`, `inference/trajectory.py`

---

### D010 — Self-skilling rung 4 (autonomous authoring) explicitly forbidden {#d010}
**Date:** 2026-06-22  
**Chose:** Cap self-skilling at rung 3 (human-gated drafting); make rung 4 an explicit non-goal in the spec.  
**Rejected:** Ship rung 4 (autonomous skill authoring) behind a feature flag, treating it like any other experimental feature.  
**Why:** Rung 4 = an agent autonomously authoring code that is then re-executed as a skill, without human review of the authored code. This violates the fail-safe-DENY policy (#4) — it creates a self-amplifying path where errors in authored skills compound across runs. A feature flag doesn't close this; even with `enabled=false` default, the pathway existing creates surface area for bypass. The right boundary is architectural exclusion, not a flag.  
**Ref:** `specs/self-skilling/`, `adaptive/macro_detector.py`

---

### D009 — DevAgent saga uses per-step compensation, not whole-tree git stash {#d009}
**Date:** 2026-06-27  
**Chose:** `_snapshot_for_write` backs up individual files pre-write; `_halt_and_compensate` unwinds in reverse (RESTORE_FILE / DELETE_FILE).  
**Rejected:** `git stash` the entire working tree before a plan run and pop it on failure.  
**Why:** A whole-tree stash would capture the user's unrelated in-progress work (uncommitted edits in other features) and pop it away on rollback — data loss risk. Per-step compensation is scoped to exactly the files the plan touched, nothing more. The git-blob backend (`DA_SAGA_GIT_BACKEND`, default OFF) closes the 256 KB file-copy cap without using the working tree or index.  
**Ref:** `specs/dev-agent-sagas/`, `inference/dev_agent.py`

---

### D008 — Tester failure is a safe-observation; it never rolls back a good write {#d008}
**Date:** 2026-06-21  
**Chose:** A failing generated pytest feeds `_reflect`/replan but the already-committed write stands.  
**Rejected:** Roll back the write on any Tester failure, treating a failing test as proof the write is wrong.  
**Why:** The Tester generates a focused test one-shot; the generated test may be wrong even when the source write is correct. Rolling back on test failure would undo valid work whenever the test generator mis-specifies the expected behavior. The safe-observation model is conservative in the right direction: the file stays, the agent replans — if the file is genuinely broken, the replan will catch it. The Tester is a signal, not a gate.  
**Ref:** `specs/dev-agent-critic/`, `inference/tester.py`

---

### D007 — Critic REVISE runs pre-disk-commit; no snapshot before Critic {#d007}
**Date:** 2026-06-21  
**Chose:** Critic sees the diff before any disk write; REVISE drives replan without snapshot/compensation.  
**Rejected:** Snapshot the file before running the Critic so a REVISE can compensate a previously-committed state.  
**Why:** The Critic runs after the lint gate but before `_snapshot_for_write` and disk commit — the file hasn't changed yet. Snapshotting at Critic time would snapshot the pre-write state, which is already the current state, making compensation a no-op. The design separates concerns cleanly: Critic = quality gate on the diff; saga = durability for disk writes. Snapshotting before the Critic would break this boundary and add overhead for zero benefit.  
**Ref:** `specs/dev-agent-critic/`, `inference/critic.py`

---

### D006 — Whole-file edit format is the default (not hashline or udiff) {#d006}
**Date:** 2026-06-20  
**Chose:** `whole_file` as `edit_format` default for all models.  
**Rejected:** `hashline` or `udiff` as default for token efficiency.  
**Why:** A/B eval verdict: hashline and udiff are efficiency plays, not correctness upgrades — they reduce tokens on large files but introduce parse/apply complexity. `whole_file` is byte-identical to the pre-ACI path, making it the zero-regression default. Structured formats are opt-in per model via `edit_format_aci.per_model`; the A/B gate confirmed keeping `whole_file` until per-model tuning is validated. Unknown format names gracefully degrade to `whole_file`.  
**Ref:** `specs/edit-format-aci/`, `inference/edit_format.py`

---

### D005 — WSL terminal routing is ON by default {#d005}
**Date:** 2026-06-21  
**Chose:** `wsl_terminal_routing.enabled: true` (default ON).  
**Rejected:** Ship default OFF, let users opt in.  
**Why:** Without WSL routing, the bwrap/firejail sandbox in `inference/sandbox.py` never actually applies on a Windows host — `RUN_TERMINAL` falls through to the allowlist-only path silently. Default OFF would mean most deployments run unsandboxed without knowing it. Default ON with graceful degradation (WSL absent or path untranslatable → native, logged) is safer. Windows-only commands (PowerShell/cmd/`*.exe`) are correctly excluded by the decision tree.  
**Ref:** `specs/wsl-terminal-routing/`, `inference/sandbox.py`

---

### D004 — L515 head-pointer: depth_comp=0 (depth compensation disabled) {#d004}
**Date:** 2026-06-23  
**Chose:** `depth_comp=0` — disable the L515 depth-to-screen-distance compensation entirely.  
**Rejected:** Keep depth compensation enabled with filtering/smoothing to reduce noise.  
**Why:** The L515 depth sensor produces ±388px rest jitter on the cursor at typical head distances — far worse than the parallax error the compensation is meant to fix. The sensor noise floor is higher than the correction benefit at the distances used (0.5–1.5m). Disabling is cleaner than filtering noisy depth through another smoothing stage. Per-axis OneEuro smoothing (`min_cutoff_y`/`beta_y`) handles the remaining pitch noise (Y is ~2× noisier than yaw/X).  
**Ref:** `feat/realsense-l515` branch, memory entry `l515-headtrack-calibration-2026-06-23.md`

---

### D003 — gemma4:12b fills the general-domain slot (gemma3:27b retired) {#d003}
**Date:** 2026-06-07  
**Chose:** `gemma4:12b` as the resident general-domain model; `gemma3:27b` retired (kept pulled for rollback).  
**Rejected (primary):** Continue using `gemma3:27b`.  
**Rejected (secondary):** Use `gemma4:12b` or `gemma4:31b` for code+plan (consolidation).  
**Why:** `gemma4:12b` (~9.1 GB) co-resides with command model + Whisper without eviction. `gemma3:27b` couldn't co-reside — triggered eviction churn on every general query. Consolidation to `gemma4` for code+plan was tested and rejected: gemma4 thinking-tax = 4× latency + 8–12k tokens vs. `qwen3-coder:30b`'s throughput. Flare fallback: `gemma4:e4b-it-qat` (smaller quantization).  
**Ref:** `inference/model_router.py`, memory entry `gemma4_general_slot_plan.md`

---

### D002 — Cloud backend is Amazon Bedrock only; direct Anthropic API removed {#d002}
**Date:** 2026-06-19  
**Chose:** Remove the direct Anthropic API path and `ANTHROPIC_API_KEY`; Bedrock is the sole cloud backend.  
**Rejected:** Maintain both paths (direct Anthropic + Bedrock) behind a config switch.  
**Why:** Dual paths create credential surface (two secret stores), divergent billing (two cost ledgers), and config complexity. Bedrock provides the same models under IAM/bearer-token auth with AWS cost consolidation. The seam is `core/cloud_backend.py`; `AWS_BEARER_TOKEN_BEDROCK` is the credential. Global prefix ensures all Bedrock model IDs are unambiguous (`us.anthropic.*`).  
**Ref:** PR #110, `core/cloud_backend.py`

---

### D001 — MOUSEDOWN/MOUSEUP execute synchronously, no asyncio.to_thread {#d001}
**Date:** (foundational — predates decision log)  
**Chose:** `MOUSEDOWN` and `MOUSEUP` verbs run synchronously in the event loop, never offloaded to a thread.  
**Rejected:** Treat them like other blocking verbs and offload to `asyncio.to_thread`.  
**Why:** These verbs are timing-critical for drag-select operations — the gap between `MOUSEDOWN` and the subsequent `MOUSEUP` or `SCROLL` must be deterministic. Offloading to a thread introduces scheduling jitter that competes with tilt/trackpad moves arriving on the same loop tick, breaking drag-select reliability. Every other blocking I/O uses `asyncio.to_thread` per AGENTS.md #2, but MOUSEDOWN/MOUSEUP are the explicit exception.  
**Ref:** `command_executor.py`, AGENTS.md §Architecture note
