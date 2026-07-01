# Decision Log

Append-only. Only log decisions with a meaningful rejected alternative — minor
implementation choices don't belong here. Keep the index under 30 lines.

See AGENTS.md Rule 12 for when and how to add entries.

---

## Index (newest first)

- [D018 — 2026-07-01 — VoiceSystemControl keeps condition/calibration switching on HybridCoordinator, not moved](#d018)
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
- [D001 — (foundational) — MOUSEDOWN/MOUSEUP execute synchronously, no asyncio.to_thread](#d001)

---

## Entries

---

### D018 — VoiceSystemControl keeps condition/calibration switching on HybridCoordinator, not moved {#d018}
**Date:** 2026-07-01
**Chose:** During the HybridCoordinator decomposition (`specs/hybrid-coordinator-decomposition/`), `_switch_condition`/`_run_calibration` stay defined on `HybridCoordinator` and are passed into `VoiceSystemControl` as narrow async delegate callables (`switch_condition`, `run_calibration`), rather than moving the method bodies into `VoiceSystemControl` itself.
**Rejected:** Move both methods' full bodies into `VoiceSystemControl` alongside the rest of the voice-keyword block they're triggered from.
**Why:** `_switch_condition` calls `self._profiler.apply_to(self._whisper, coordinator=self)` — an external `ConditionProfiler` API that takes the *real* `HybridCoordinator` instance as a `coordinator=` kwarg. Moving the method into `VoiceSystemControl` would force it to hold a back-reference to the full coordinator, violating the module's explicit-DI requirement (spec R3 — no back-reference, only named accessor callables). Leaving the method on the coordinator and injecting it as a delegate satisfies both the external API contract and the DI requirement; the fallback matches spec R4.3 ("if a branch can't be cleanly attributed, leave it in HybridCoordinator").
**Ref:** `core/hybrid_coordinator.py` (`_switch_condition`, `_run_calibration`), `core/voice_system_control.py`

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
