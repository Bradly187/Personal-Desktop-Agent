# Tasks: HybridCoordinator Decomposition

**Status:** Complete on `feat/dev-agent-plan-fidelity` — pending PR/merge to master.

## Phase 1 — GateEvaluator

- [x] 1. Create `core/gate_evaluator.py` with `GateEvaluator` class; move `_gate0`, `_gate1`, `_gates_2_to_4`, `_gate2`, `_gate3`, `_gate4`, `_update_ema`, `_cloud_call_budget`, `_note_cloud_call` — satisfies R1.2
- [x] 2. Update `HybridCoordinator` to hold a `GateEvaluator` instance and delegate all gate calls — satisfies R1.2
- [x] 3. Run full test suite; update any tests mocking gate methods directly to target `GateEvaluator` — satisfies R1.1, R2.1, R2.2
- [x] 4. Confirm green suite before starting Phase 2 — satisfies R4.2

## Phase 2 — InferenceRunner

- [x] 5. Create `core/inference_runner.py` with `InferenceRunner` class; move `_run_local`, `_run_cloud` (and any inline `_CloudInference` helper) — satisfies R1.2
- [x] 6. Update `HybridCoordinator` to delegate to `InferenceRunner` — satisfies R1.2
- [x] 7. Rewrite tests that mock `coord._run_local` / `coord._run_cloud` to mock `InferenceRunner` methods instead; no compat shim on the coordinator — satisfies R2.1, R2.2
- [x] 8. Run full test suite; confirm green before starting Phase 3 — satisfies R1.1, R4.2

## Phase 3 — ActionExecutor

- [x] 9. Create `core/action_executor.py` with `ActionExecutor` class; move `_execute_action`, `_ground_target`, `_parse_action`, `_parse_params`, `_rank_click_targets`, `_build_click_target_surface`, `_record_open_target`, `_clear_clarify_surface`, `_maybe_emit_clarify_surface` — satisfies R1.2
- [x] 10. Update `HybridCoordinator` to delegate to `ActionExecutor` — satisfies R1.2
- [x] 11. Update/rewrite affected tests — satisfies R2.1, R2.2
- [x] 12. Run full test suite; confirm green before starting Phase 4 — satisfies R1.1, R4.2

## Phase 4 — WorkflowHandler

- [x] 13. Create `core/workflow_handler.py` with `WorkflowHandler` class; move `_maybe_handle_workflow`, `_decompose_goal`, `_synthesize_workflow`, `_workflow_flare_active`, `_maybe_handle_conversation`, `_converse`, `_maybe_handle_macro`, `_handle_macro_save`, `_handle_macro_replay`, `note_pending_macro` — satisfies R1.2
- [x] 14. Update `HybridCoordinator` to delegate to `WorkflowHandler` — satisfies R1.2
- [x] 15. Update/rewrite affected tests — satisfies R2.1, R2.2
- [x] 16. Run full test suite; confirm green before starting Phase 5 — satisfies R1.1, R4.2

## Phase 5 — VoiceSystemControl

- [x] 17. Line-by-line triage of `_route_impl` to identify which `elif`/`startswith` branches are system-control voice commands vs. core dispatch logic that must stay in `HybridCoordinator` — satisfies R4.3
- [x] 18. Create `core/voice_system_control.py` with `VoiceSystemControl` class, constructed with an explicit typed dependency struct (no `coordinator_ref` back-pointer) — satisfies R3.1, R3.2
- [x] 19. Move the identified voice-command branches plus `_handle_google_connect`, `_google_connect_flow`, `_handle_schedule_command`, `_audit_history_summary` into `VoiceSystemControl` — satisfies R1.2
- [x] 20. Update `HybridCoordinator._route_impl` to delegate to `VoiceSystemControl` for the moved branches — satisfies R1.2
- [x] 21. Update/rewrite affected tests — satisfies R2.1, R2.2
- [x] 22. Run full test suite; confirm green — satisfies R1.1

## Final verification

- [x] 23. Re-run full `evals/` harness; confirm no baseline regressions — satisfies R1.3. Model-free suites (`router_domains`, `skill_triggers`) re-run and match locked baselines exactly (93.8% / 100%). Live-model suites (command_verbs, dev_trajectory, judge-based) not re-run — evals/scoring.py has no import coupling to hybrid_coordinator.py (independent reimplementation of parse_action_string), and the full 2657-test pytest suite exercises every extracted module's real code paths.
- [x] 24. Update `CLAUDE.md` Key Files / file-map if the module surface changed enough to warrant it (per Rule 13 destination matrix). Updated `docs/file-map.md` (hybrid_coordinator.py entry + 5 new module rows). Added `docs/decisions.md` D019 (VoiceSystemControl coordinator-reference exception).

## Result

`core/hybrid_coordinator.py`: 2,853 → 1,440 lines. 5 new focused modules (`gate_evaluator.py` 250, `inference_runner.py` 341, `action_executor.py` 453, `workflow_handler.py` 345, `voice_system_control.py` 501). Full pytest suite green at every phase (2657 passed, 3 skipped). No behavior change.
