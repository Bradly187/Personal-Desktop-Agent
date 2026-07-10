# Spec: Initialize `IPadBridge._coordinator` in `__init__` — B6

**Status:** In Progress
**Approved:** Brad, 2026-07-09
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

`IPadBridge.__init__` (`core/ipad_bridge.py:154-211`) initialises all optional
phase-2+ components (`_fusion`, `_lidar`, `_gesture`, `_whisper`, `_viewer`)
explicitly to `None`, following the repo convention for components wired by
`main.py`. However, **`_coordinator` is never initialised** — it exists only
after `set_coordinator()` is called by `main.py`.

The advertised standalone entry point (`python ipad_bridge.py`) never calls
`set_coordinator`. If an iPad client connects in standalone mode and sends any
of the following message types, `_handle_message` dereferences `self._coordinator`
and raises `AttributeError`, which tears down the WebSocket receive loop:

- `pain_day_override` (`ipad_bridge.py:529`)
- `flare_profile` (`:551`)
- `calibration_start` (`:561`)
- `calibration_cancel` (`:569`)

The fix is a one-line addition to `__init__`: `self._coordinator = None`.
The existing message handlers already guard on the coordinator being `None`-like
in some branches; a safe `None` sentinel lets them handle it gracefully instead
of crashing.

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B6.

---

## 2. Glossary

- **`IPadBridge`**: WebSocket server in `core/ipad_bridge.py` that receives
  sensor and control messages from the iPad app.
- **`_coordinator`**: Reference to `HybridCoordinator`, wired by `main.py` via
  `set_coordinator()`. Required for pain-day/calibration message forwarding.
- **`_handle_message`**: The async message dispatch method; crashes on the above
  message types when `_coordinator` is undefined.
- **Standalone mode**: Running `python ipad_bridge.py` directly without the
  full `main.py` pipeline — used for hardware testing without LLM/coordinator.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: `_coordinator` initialised in `__init__`

**User Story:** As Brad, I want the iPad bridge standalone mode to not crash
when my iPad sends a pain-day or calibration message, so that I can test the
hardware link without starting the full pipeline.

#### Acceptance Criteria
1. `IPadBridge.__init__` SHALL initialise `self._coordinator = None` before any
   method that reads `self._coordinator` can be called.
2. THE type annotation SHALL be consistent with the existing pattern:
   `self._coordinator: Optional["HybridCoordinator"] = None`.
3. WHEN `_coordinator` is `None` and a `pain_day_override`, `flare_profile`,
   `calibration_start`, or `calibration_cancel` message is received, THE
   `_handle_message` method SHALL NOT raise `AttributeError`.
4. WHEN `_coordinator` is `None` and one of the above messages is received,
   THE bridge SHALL log a warning and return a graceful error response to the
   iPad client (e.g. `{"status": "error", "reason": "coordinator not available"}`).
5. WHEN `_coordinator` is wired (normal `main.py` boot), THE behaviour of all
   four message types SHALL be identical to today.

### Requirement 2: No new crashes from `None` coordinator

#### Acceptance Criteria
1. FOR ALL message types handled in `_handle_message`, THE handler SHALL either
   (a) guard on `self._coordinator is None` before dereferencing it, or (b) not
   reference `self._coordinator` at all.
2. THE existing guard patterns already in `_handle_message` (e.g. `if self._fusion`)
   SHALL serve as the template — apply the same pattern to `_coordinator`-gated blocks.

---

## 4. Technical Design

**`core/ipad_bridge.py:__init__`** — add after the `_viewer` line (≈ line 178):

```python
# Phase 2+ coordinator (wired by main.py via set_coordinator)
self._coordinator: Optional["HybridCoordinator"] = None
```

**`core/ipad_bridge.py:_handle_message`** — for each of the four crashing message
types, wrap the coordinator dereference:

```python
if self._coordinator is None:
    log.warning("IPadBridge: coordinator not available for %s", msg_type)
    return {"status": "error", "reason": "coordinator not available"}
```

(Check whether any of the four blocks already have such a guard before adding it.)

- **No schema changes.** No VRAM impact. No Swift bridge changes (the message
  types and JSON shapes are unchanged; only the server-side error path changes).
- **`set_coordinator` method:** no changes needed — it already overwrites the
  attribute.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_ipad_bridge.py` (add cases):
  - R1.1: instantiate `IPadBridge`; assert `hasattr(bridge, "_coordinator")` is `True`.
  - R1.3: call `_handle_message({"type": "pain_day_override", ...})` on an
    un-wired bridge; assert no `AttributeError` is raised.
  - R1.4: assert the return value contains `"status": "error"` when coordinator is `None`.
  - R1.5: wire a mock coordinator; assert `pain_day_override` is forwarded normally.

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Add `self._coordinator = None` to `IPadBridge.__init__` — satisfies R1.1, R1.2
- [ ] 2. Add `None`-guard to `pain_day_override` handler — satisfies R1.3, R1.4, R2.1
- [ ] 3. Add `None`-guard to `flare_profile` handler — satisfies R1.3, R1.4, R2.1
- [ ] 4. Add `None`-guard to `calibration_start` handler — satisfies R1.3, R1.4, R2.1
- [ ] 5. Add `None`-guard to `calibration_cancel` handler — satisfies R1.3, R1.4, R2.1
- [ ] 6. Add unit tests — satisfies R1.1, R1.3, R1.4, R1.5
- [ ] 7. Run full test suite; confirm green
