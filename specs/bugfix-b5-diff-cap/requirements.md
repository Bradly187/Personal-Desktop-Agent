# Spec: Restore `_CONFIRM_DIFF_MAX_LINES` to spec'd 400 — B5

**Status:** In Progress
**Approved:** Brad, 2026-07-09
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

The approval-card diff shown to Brad during voice-gated file writes is capped by
`_CONFIRM_DIFF_MAX_LINES`. The spec (`specs/chat-workbench-parity/requirements.md:259`)
mandates **400 lines**. After PR #164 split `DevAgent` into `StepExecutor`,
the constant was forked:

| Location | Value | Status |
|---|---|---|
| `inference/step_executor.py:34` | `100` | **Live** — used at lines 297-299 |
| `inference/dev_agent.py:879` | `400` | **Dead** — defined as a class attribute, never referenced in step execution |

The live copy truncates approval diffs **4× harder** than specified. A 300-line
diff (e.g. a real refactor) is shown to Brad as only 100 lines, making an
informed voice-approval decision impossible. The fix: set the live constant to
400 and delete the dead class-level copy.

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B5;
spec `specs/chat-workbench-parity/requirements.md R5.1`.

---

## 2. Glossary

- **`_CONFIRM_DIFF_MAX_LINES`**: Maximum lines of diff shown in an approval card
  before truncation with a "… N lines dropped" notice.
- **Approval card**: The rich diff block surfaced to Brad (via TTS/iPad) for
  voice-gated `WRITE_FILE` / `EDIT_FILE` steps.
- **`step_executor.py:_execute_step`**: The function that builds and presents
  the approval card; uses the module-level constant at line 297-299.
- **Dead copy**: `DevAgent._CONFIRM_DIFF_MAX_LINES = 400` (class attribute,
  `dev_agent.py:879`) — defined but unreferenced in any truncation logic.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Live constant matches spec

**User Story:** As Brad, I want to see up to 400 lines of diff in an approval
card, so that I can make an informed decision before authorising a large file
change by voice.

#### Acceptance Criteria
1. THE module-level `_CONFIRM_DIFF_MAX_LINES` in `inference/step_executor.py`
   SHALL be `400`.
2. WHEN a diff has ≤ 400 lines, THE approval card SHALL show the full diff.
3. WHEN a diff has > 400 lines, THE approval card SHALL show the first 400 lines
   followed by a "… N lines dropped" notice (unchanged truncation logic).
4. THE dead class-level `_CONFIRM_DIFF_MAX_LINES = 400` in `dev_agent.py:879`
   SHALL be removed to eliminate the split-brain constant.
5. THE codebase SHALL contain exactly **one** definition of `_CONFIRM_DIFF_MAX_LINES`.

### Requirement 2: No regression on truncation behaviour

#### Acceptance Criteria
1. THE truncation notice format (e.g. `f"… {dropped} lines dropped"`) SHALL
   remain unchanged.
2. THE approval flow (voice-gate, RMS threshold, deadline) SHALL be unaffected
   by this change.

---

## 4. Technical Design

**`inference/step_executor.py:34`:** Change `100` → `400`.

```python
_CONFIRM_DIFF_MAX_LINES = 400
```

**`inference/dev_agent.py:879`:** Remove the class attribute:

```python
# DELETE this line:
_CONFIRM_DIFF_MAX_LINES = 400
```

No other files reference this constant. No schema changes. No VRAM impact.
No Swift bridge changes.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_step_executor.py` (add/extend):
  - R1.1: assert `step_executor._CONFIRM_DIFF_MAX_LINES == 400`.
  - R1.2: mock a 200-line diff; assert approval card shows all 200 lines (no truncation).
  - R1.3: mock a 500-line diff; assert approval card shows 400 lines + truncation notice.
  - R1.5: assert only one module defines `_CONFIRM_DIFF_MAX_LINES` (import check).
- **Spec cross-check:** verify `specs/chat-workbench-parity/requirements.md:259`
  still cites 400; update if the spec was itself wrong.

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Change `step_executor.py:34`: `100` → `400` — satisfies R1.1
- [ ] 2. Delete `dev_agent.py:879` class attribute — satisfies R1.4, R1.5
- [ ] 3. Add unit tests — satisfies R1.1, R1.2, R1.3, R1.5
- [ ] 4. Run full test suite; confirm green
