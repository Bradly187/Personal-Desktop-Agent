# Tasks — B4: Add undo phrases to `_SYSTEM_CONTROL_PHRASES`

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b4-undo-phrases/requirements.md`
> File: `core/voice_system_control.py`

---

## Tasks

- [x] 1. **Define `_UNDO_PHRASES` constant** — add before `_SYSTEM_CONTROL_PHRASES` at the top of `voice_system_control.py`:
  ```python
  _UNDO_PHRASES: frozenset[str] = frozenset({
      "undo that run", "undo run", "revert run",
      "undo last task", "revert last task",
      "undo the run", "undo the last run", "undo task",
  })
  ```
  Satisfies R1.4

- [x] 2. **Merge into `_SYSTEM_CONTROL_PHRASES`** — append `| _UNDO_PHRASES` at the closing brace of the frozenset literal (line 80).
  ```python
  _SYSTEM_CONTROL_PHRASES: frozenset[str] = frozenset({
      # ... existing phrases unchanged ...
  }) | _UNDO_PHRASES
  ```
  Satisfies R1.1, R1.2

- [x] 3. **Update `maybe_handle` undo `elif`** — replace the raw string tuple in the undo branch (line 306-307) with `elif _lower in _UNDO_PHRASES:`.
  Satisfies R1.4

- [x] 4. **Remove "KEEP IN SYNC" comment** — lines 44-46; replace with a one-line reference to `_UNDO_PHRASES` explaining the structural enforcement.
  Satisfies R3.1, R3.2

- [x] 5. **Add unit tests** — `tests/test_voice_system_control.py` (add cases):
  - For each phrase in `_UNDO_PHRASES`: assert `_is_system_control_voice(cmd)` is `True` (R1.3)
  - For existing phrases ("pain day on", "stop agent"): assert still `True` (R2.1)
  - Assert `_UNDO_PHRASES.issubset(_SYSTEM_CONTROL_PHRASES)` (R1.4 structural)
  - Assert `_is_system_control_voice` returns `False` for `source="touch"` (guard)

- [x] 6. **Add integration/eval case** — inject `Command(text="undo that run", source="voice")` into `route_impl` mock; assert returned action is `REVERT_RUN`, not domain-classifier result (R2.2).

  **Result (2026-07-10):** `tests/test_voice_system_control.py::TestUndoRoutingIntegration`
  drives the REAL `EventDispatcher.route_impl` via `HybridCoordinator.route` with only
  the DevAgent leaf mocked (the coordinator-wired `_voice_control` is real). Two cases:
  (1) `Command("undo that run", source="voice")` → `result["action"] == "REVERT_RUN"`,
  `offered is True`, and `_dev_agent.handle` **not** awaited (dev pre-gate skipped);
  (2) contrast — `Command("refactor the auth module", source="voice")` → `action ==
  "dev_agent"`, `handle` awaited once, proving the undo interception is targeted, not a
  blanket short-circuit. Both green.

- [x] 7. **Run full test suite** — `pytest -x`; confirm green.
