# Tasks — B6: Initialize `IPadBridge._coordinator` in `__init__`

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b6-ipad-coordinator/requirements.md`
> File: `core/ipad_bridge.py`

---

## Tasks

- [x] 1. **Add `_coordinator = None` to `IPadBridge.__init__`** — insert after the `_viewer` assignment (≈ line 178), following the existing `Optional["X"] = None` pattern:
  ```python
  # Phase 2+ coordinator (wired by main.py via set_coordinator)
  self._coordinator: Optional["HybridCoordinator"] = None
  ```
  Satisfies R1.1, R1.2

- [x] 2. **Add `None`-guard to `pain_day_override` handler** (≈ line 529) — before the coordinator dereference:
  ```python
  if self._coordinator is None:
      log.warning("IPadBridge: coordinator not wired, ignoring pain_day_override")
      return {"status": "error", "reason": "coordinator not available"}
  ```
  Satisfies R1.3, R1.4, R2.1

- [x] 3. **Add `None`-guard to `flare_profile` handler** (≈ line 551) — same pattern.
  Satisfies R1.3, R1.4, R2.1

- [x] 4. **Add `None`-guard to `calibration_start` handler** (≈ line 561) — same pattern.
  Satisfies R1.3, R1.4, R2.1

- [x] 5. **Add `None`-guard to `calibration_cancel` handler** (≈ line 569) — same pattern.
  Satisfies R1.3, R1.4, R2.1

- [x] 6. **Add unit tests** — `tests/test_ipad_bridge.py` (add cases):
  - Instantiate `IPadBridge()`; assert `hasattr(bridge, "_coordinator")` is `True` (R1.1)
  - Instantiate `IPadBridge()`; assert `bridge._coordinator is None` (R1.2)
  - Call `_handle_message({"type": "pain_day_override", ...})` on un-wired bridge; assert no `AttributeError` (R1.3)
  - Assert return dict contains `"status": "error"` and `"reason"` key (R1.4)
  - Wire a mock coordinator; assert `pain_day_override` is forwarded normally (R1.5)
  - Repeat for `flare_profile`, `calibration_start`, `calibration_cancel`

- [x] 7. **Run full test suite** — `pytest -x`; confirm green.
