# Spec: GAP-4: Staleness check on resume seed and replayed reads

---

## 1. Background — the "Why"

`resume_pending_plan` injects `WorkingMemory` derived from `agent_steps` with no check that the filesystem still matches — a file modified between crash and resume is silently misrepresented.

**Status:** Shipped (PR #___)
**Approved:** Brad, 2026-07-03

---

## 2. Glossary

- **Resume Seed Context**: The context populated into the prompt when a plan resumes after a crash.
- **Working Memory**: The stored set of file paths, notes, and context from previous steps.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Staleness Check

**User Story:** As Brad, I want the system to check if files have changed before resuming a plan, so that the agent doesn't act on stale context.

#### Acceptance Criteria
1. WHEN `DA_RESUME_STALENESS` is ON, THE resume seed process SHALL stat each path in `WorkingMemory.files`.
2. THE process SHALL annotate entries changed since the step timestamp as `[STALE — modified after run]`.
3. THE process SHALL drop stale notes derived from those files.
4. IF a stat failure occurs (e.g., deleted file), THEN THE entry SHALL be marked as stale instead of raising an error.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/working_memory.py`, `inference/dev_agent.py` (`_resume_seed_context`).

### Configuration (flat YAML)

```yaml
gap-4-resume-staleness:
  enabled: false          # DA_RESUME_STALENESS
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - Test touch file after step timestamp ⇒ seed contains STALE marker.

---

## 6. Tasks

- [x] 1. Add `DA_RESUME_STALENESS` to `core/flags.py`.
- [ ] 2. Update `summarize_run` in `inference/working_memory.py`.
- [ ] 3. Update `_resume_seed_context` in `inference/dev_agent.py`.
