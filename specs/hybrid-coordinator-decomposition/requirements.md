# Spec: HybridCoordinator Decomposition

## 1. Background — the "Why"

`core/hybrid_coordinator.py` has grown to 2,853 lines, with `_route_impl` alone
at 768 lines. This is a maintainability refactor, not a new user-facing
capability: gate logic, inference dispatch, action execution, workflow/macro
handling, and voice-keyword system commands are all interleaved in one class,
making each hard to test or modify in isolation. No behavior change is
intended — this spec exists to gate the *structure* of the change per AGENTS.md
Rule 11, since it touches a core pipeline file at scale.

**Status:** Building — all 5 phases complete on `feat/dev-agent-plan-fidelity`, pending PR/merge to master.
**Approved:** Brad, 2026-07-01
**Owner / author session:** Claude Code

---

## 2. Glossary

- **HybridCoordinator**: `core/hybrid_coordinator.py` — routes `Command`s through gates, inference, and action execution; the class being decomposed.
- **GateEvaluator**: new `core/gate_evaluator.py` — Gate 0–4 decision logic (VRAM/EMA/cloud-budget checks) extracted from `_gate0`..`_gate4`, `_gates_2_to_4`, `_update_ema`, `_cloud_call_budget`, `_note_cloud_call`.
- **InferenceRunner**: new `core/inference_runner.py` — local/cloud model dispatch extracted from `_run_local`, `_run_cloud`.
- **ActionExecutor**: new `core/action_executor.py` — verb execution, target grounding, click-target ranking/surfacing extracted from `_execute_action`, `_ground_target`, `_parse_action`, `_parse_params`, `_rank_click_targets`, `_build_click_target_surface`, `_record_open_target`, `_clear_clarify_surface`, `_maybe_emit_clarify_surface`.
- **WorkflowHandler**: new `core/workflow_handler.py` — workflow/goal decomposition, conversation mode, and macro save/replay extracted from `_maybe_handle_workflow`, `_decompose_goal`, `_synthesize_workflow`, `_workflow_flare_active`, `_maybe_handle_conversation`, `_converse`, `_maybe_handle_macro`, `_handle_macro_save`, `_handle_macro_replay`, `note_pending_macro`.
- **VoiceSystemControl**: new `core/voice_system_control.py` — keyword/verb-matched system-control voice commands triaged out of `_route_impl`, plus `_handle_google_connect`, `_google_connect_flow`, `_handle_schedule_command`, `_audit_history_summary`. Wired via explicit dependency injection (a typed struct of only the specific coordinator state/methods it needs) — no back-reference to the full `HybridCoordinator` instance.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Behavior-preserving extraction

**User Story:** As Brad, I want the coordinator split into focused modules without changing runtime behavior, so that the codebase stays maintainable without risking regressions.

#### Acceptance Criteria
1. THE full existing test suite SHALL pass, unmodified in assertions (only mock targets may change per R2), after each phase.
2. FOR ALL five extracted modules (GateEvaluator, InferenceRunner, ActionExecutor, WorkflowHandler, VoiceSystemControl), `HybridCoordinator` SHALL delegate to the extracted module rather than duplicating logic.
3. THE eval baselines in `evals/baselines/` SHALL be re-run and SHALL NOT regress after the full decomposition (all 5 phases) lands.

### Requirement 2: Test mocks follow the extraction

**User Story:** As a future maintainer, I want tests to mock the new module boundaries, so that tests reflect the real call graph.

#### Acceptance Criteria
1. WHEN a method (e.g. `_run_local`) moves to an extracted module, THE tests that previously mocked `coord._run_local` SHALL be rewritten in the same phase to mock the new module's method directly.
2. THE repo SHALL NOT retain a backward-compatible delegating property (e.g. `coord._run_local = ...`) purely to avoid updating test mocks.

### Requirement 3: No hidden god-object dependency in VoiceSystemControl

**User Story:** As a future maintainer, I want `VoiceSystemControl` to declare exactly what it depends on, so that its coupling to `HybridCoordinator` is visible and auditable.

#### Acceptance Criteria
1. `VoiceSystemControl` SHALL receive its dependencies (e.g. `writable_roots`, `tts_speak`, `capability_summary`) as explicit constructor parameters or a typed dependency struct.
2. `VoiceSystemControl` SHALL NOT hold a reference to the full `HybridCoordinator` instance.

### Requirement 4: Phased, independently verifiable rollout

**User Story:** As Brad, I want each extraction phase to land and verify independently, so that a regression is easy to bisect.

#### Acceptance Criteria
1. THE extraction SHALL proceed in this order: GateEvaluator → InferenceRunner → ActionExecutor → WorkflowHandler → VoiceSystemControl.
2. WHEN a phase's extraction and test updates are complete, THE full test suite SHALL be run and SHALL be green before the next phase begins.
3. IF `_route_impl`'s voice-command branches cannot be cleanly attributed to VoiceSystemControl during Phase 5 triage, THEN THE ambiguous branch SHALL remain in `HybridCoordinator` rather than being force-extracted.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** No change to the `route()` / `_route_impl` external contract — `Command` in, same dispatch semantics out. This is an internal-structure-only change.
- **New `Command` fields:** None.
- **Models / VRAM:** No change — `InferenceRunner` calls the same local/cloud model paths `_run_local`/`_run_cloud` already used.
- **Persistence:** No schema change.
- **Cross-platform:** Not applicable — no WebSocket payload changes.

Remaining on `HybridCoordinator` after all 5 phases: `route`, `_route_impl` (slimmed to dispatch), `__init__`, the `set_*` DI setters, correction/drift handling (`_on_correction`, `_note_intent_drift`, `_harvest_correction`), `correct`, `get_status`. Estimated ~500–600 lines.

---

## 5. Behavior Verification

- **Unit/integration tests:** existing `tests/` suite is the primary gate (R1.1); no new test scenarios are needed since no new behavior is introduced. Tests mocking moved methods are updated per R2.
- **Eval suite:** re-run full `evals/` harness after Phase 5 (R1.3); no new eval cases needed since this is not a behavior change.

---

## 6. Tasks

See `tasks.md`.
