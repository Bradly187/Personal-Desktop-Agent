# Spec: Add undo phrases to `_SYSTEM_CONTROL_PHRASES` — B4

**Status:** In Progress
**Approved:** Brad, 2026-07-09
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

`core/voice_system_control.py` handles "undo that run" and related revert
phrases in `VoiceSystemControl.maybe_handle` (lines 306-316). However,
`_SYSTEM_CONTROL_PHRASES` (lines 47-80) — the frozenset used by
`_is_system_control_voice()` to short-circuit the dev-agent pre-gate in
`event_dispatcher.py:62` — **does not include any of the undo phrases**.

The consequence: when Brad says "undo that run" or "undo last task", the
`DomainClassifier` may classify it as a `dev` or `general` domain command and
the EventDispatcher sends it to DevAgent (or the LLM) instead of the
`maybe_handle` rewind handler. The file's own comment at line 44-46 explicitly
warns: *"KEEP IN SYNC with the keyword block in route()"* — that sync was
missed when the undo phrases were added.

This is an accessibility-critical gap: voice-initiated rollback is the primary
"undo" mechanism for a user who cannot easily operate a keyboard. A misrouted
undo may silently do nothing or — worse — be interpreted as a dev task.

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B4;
spec `specs/voice-invokable-rewind/`.

---

## 2. Glossary

- **`_SYSTEM_CONTROL_PHRASES`**: Frozenset at `voice_system_control.py:47`.
  Controls which voice phrases bypass the dev-agent pre-gate.
- **`_is_system_control_voice`**: Function at `voice_system_control.py:82` that
  checks if a `Command` matches `_SYSTEM_CONTROL_PHRASES` (plus regex fallbacks).
- **`maybe_handle`**: The keyword-dispatch method in `VoiceSystemControl` that
  handles system-control phrases, including the undo/revert block at line 306.
- **`VoiceRewindHandler` / `revert_last_run`**: The `DevAgent.revert_last_run()`
  method invoked by the undo handler at line 314.
- **Dev pre-gate**: The block at `event_dispatcher.py:60-68` that diverts
  non-system-control voice commands to the DevAgent domain classifier.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Undo phrases present in `_SYSTEM_CONTROL_PHRASES`

**User Story:** As Brad, I want saying "undo that run" or "undo last task" to
immediately trigger the revert handler, so that I can roll back an agent run
without the phrase being misrouted to the LLM or DevAgent.

#### Acceptance Criteria
1. THE `_SYSTEM_CONTROL_PHRASES` frozenset SHALL contain every phrase listed in
   the `maybe_handle` undo/revert `elif` block at `voice_system_control.py:306-307`.
2. THE set SHALL include at minimum: `"undo that run"`, `"undo run"`,
   `"revert run"`, `"undo last task"`, `"revert last task"`, `"undo the run"`,
   `"undo the last run"`, `"undo task"`.
3. WHEN `_is_system_control_voice` is called with any of the above phrases,
   THE function SHALL return `True`.
4. THE `_SYSTEM_CONTROL_PHRASES` frozenset and the `maybe_handle` undo `elif`
   phrase list SHALL be derived from or validated against the **same Python
   constant** (a `frozenset` defined once, reused in both places) to prevent
   future drift.

### Requirement 2: No regression on existing phrases

#### Acceptance Criteria
1. FOR ALL phrases currently in `_SYSTEM_CONTROL_PHRASES`, THE
   `_is_system_control_voice` function SHALL continue to return `True`.
2. WHEN an undo phrase arrives with `source="voice"` or `source="voice_local"`,
   THE `event_dispatcher.route_impl` SHALL not invoke the DomainClassifier or
   DevAgent for that command.

### Requirement 3: "KEEP IN SYNC" comment replaced by structural enforcement

#### Acceptance Criteria
1. THE comment at `voice_system_control.py:44-46` ("KEEP IN SYNC with the
   keyword block in route()") SHALL be updated to reference the shared constant.
2. IF the shared-constant approach (R1.4) is used, THE "KEEP IN SYNC" comment
   SHALL be removed as it is no longer needed.

---

## 4. Technical Design

**Preferred approach — shared constant:**

Define a module-level `_UNDO_PHRASES: frozenset[str]` constant:
```python
_UNDO_PHRASES: frozenset[str] = frozenset({
    "undo that run", "undo run", "revert run",
    "undo last task", "revert last task",
    "undo the run", "undo the last run", "undo task",
})
```

Then:
1. Include `_UNDO_PHRASES` in `_SYSTEM_CONTROL_PHRASES`:
   ```python
   _SYSTEM_CONTROL_PHRASES: frozenset[str] = frozenset({
       # ... existing phrases ...
   }) | _UNDO_PHRASES
   ```
2. In `maybe_handle`, replace the raw string tuple in the `elif` with:
   ```python
   elif _lower in _UNDO_PHRASES:
   ```

This eliminates the "KEEP IN SYNC" surface entirely.

**Alternative (minimal diff):** Simply add the undo phrases directly to
`_SYSTEM_CONTROL_PHRASES` and update the comment. Lower structural value but
still fixes the routing bug.

- **No schema changes.** No VRAM impact. No Swift bridge changes.
- **No import changes** — `_UNDO_PHRASES` stays in `voice_system_control.py`.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_voice_system_control.py` (add cases):
  - R1.3: for each undo phrase, assert `_is_system_control_voice(cmd)` is `True`.
  - R2.1: for existing phrases (e.g. "pain day on"), assert still `True`.
  - R1.4 structural: assert `_UNDO_PHRASES` is a subset of `_SYSTEM_CONTROL_PHRASES`
    (or that the `elif` check uses the same set).
- **Eval / integration:** Inject a mock voice command `"undo that run"` into
  `route_impl`; assert the return action is `REVERT_RUN`, not a domain-classifier
  result.

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Define `_UNDO_PHRASES` constant in `voice_system_control.py` — satisfies R1.4
- [ ] 2. Merge into `_SYSTEM_CONTROL_PHRASES` via `|` — satisfies R1.1, R1.2
- [ ] 3. Update `maybe_handle` `elif` to use `_lower in _UNDO_PHRASES` — satisfies R1.4
- [ ] 4. Remove or update "KEEP IN SYNC" comment — satisfies R3.1, R3.2
- [ ] 5. Add unit tests — satisfies R1.3, R2.1, R1.4
- [ ] 6. Add integration/eval case — satisfies R2.2
- [ ] 7. Run full test suite; confirm green
