# Tasks — B5: Restore `_CONFIRM_DIFF_MAX_LINES` to 400

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b5-diff-cap/requirements.md`

---

## Tasks

- [x] 1. **`inference/step_executor.py:34`** — change live constant:
  ```python
  # Before:
  _CONFIRM_DIFF_MAX_LINES = 100
  # After:
  _CONFIRM_DIFF_MAX_LINES = 400   # spec: chat-workbench-parity R5.1
  ```
  Satisfies R1.1

- [x] 2. **`inference/dev_agent.py:879`** — delete dead class attribute:
  ```python
  # Delete this line (and surrounding blank lines if left orphaned):
  _CONFIRM_DIFF_MAX_LINES = 400
  ```
  Satisfies R1.4, R1.5

- [x] 3. **Add unit tests** — `tests/test_step_executor.py` (add cases):
  - Assert `step_executor._CONFIRM_DIFF_MAX_LINES == 400` (R1.1)
  - Mock a 200-line diff → approval card shows all 200 lines, no truncation marker (R1.2)
  - Mock a 500-line diff → approval card shows 400 lines + `"… 100 lines dropped"` notice (R1.3)
  - Assert only one module defines `_CONFIRM_DIFF_MAX_LINES` (import check) (R1.5)

- [x] 4. **Verify `specs/chat-workbench-parity/requirements.md:259`** — confirm it still cites 400; update if discrepant.

- [x] 5. **Run full test suite** — `pytest -x`; confirm green.
