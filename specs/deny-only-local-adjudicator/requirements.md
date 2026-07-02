# Spec: Deny-Only Local Adjudicator for Queued Escalations (CG-4)

## 1. Background — the "Why"
When `DevAgent` exhausts its replan or step budget, it rolls back changes and pushes the failed goal to the `dev_escalations` queue for human review. However, many of these failures are due to the model going down a hallucinated rabbit hole or attempting an impossible constraint. Pinging the user to "review" these dead-ends wastes human attention. 

A "deny-only" local adjudicator uses a fast local model to triage these pending escalations. If the model determines the failure is a trivial dead-end or hallucination, it auto-dismisses (denies) the escalation. It can **never** auto-approve or execute code on the user's behalf; it acts purely as a noise filter for the human review queue.

**Status:** In Progress
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Glossary
- **Deny-Only Adjudicator**: A lightweight background component that evaluates `pending` items in the `dev_escalations` queue.
- **Auto-Dismissed**: A new resolution status in `agent.db` for escalations that the adjudicator deemed unnecessary for human review.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Adjudication Logic
**User Story:** As Brad, I want the system to filter out useless escalations so I only review things that actually require my decision.
1. THE `LocalAdjudicator` SHALL evaluate `pending` escalations using a local model (`qwen3-coder:30b` or the default plan model).
2. THE `LocalAdjudicator` SHALL prompt the model with the goal, the failure reason, and the failure detail/trajectory.
3. THE model SHALL output a strict decision: either `DISMISS` (trivial, hallucination, safe to drop) or `ESCALATE` (needs human eyes).
4. IF the model outputs `DISMISS`, THEN THE `LocalAdjudicator` SHALL update the escalation status to `auto_dismissed`.
5. IF the model outputs `ESCALATE` or fails to parse, THEN THE `LocalAdjudicator` SHALL leave the status as `pending`.

### Requirement 2: Asynchronous Processing
**User Story:** As Brad, I don't want the agent blocking on this adjudication during normal execution.
1. THE adjudication SHALL run asynchronously, either immediately after `insert_escalation` (without blocking the rollback) or as a background consumer in the `ProactiveScheduler`.
2. THE `ProactiveScheduler._maybe_nudge_escalations` SHALL only count escalations that are still `pending` (i.e. not auto-dismissed).

### Requirement 3: Safety & Observability
**User Story:** As Brad, I want this feature to fail-safe so I never lose important escalations.
1. THE feature SHALL be gated by a new configuration flag `DA_AUTO_ADJUDICATE`, defaulting to `0` (OFF) until tested.
2. WHEN `DA_AUTO_ADJUDICATE` is OFF, the adjudicator SHALL NOT run, and all escalations remain `pending`.
3. IF the adjudicator throws an exception, the escalation SHALL fail-safe to `pending`.

---

## 4. Technical Design

- **Entry point:** `core/proactive_scheduler.py` before nudging, calling `inference.adjudicator.LocalAdjudicator`.
- **New DB Status:** `auto_dismissed` inside the `dev_escalations` table.
- **Model / VRAM:** Uses the existing local plan model via `ModelRouter`.

### Configuration
```yaml
auto_adjudicate:
  enabled: false   # DA_AUTO_ADJUDICATE
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** `tests/test_local_adjudicator.py`
  - Assert OFF flag skips processing.
  - Assert `DISMISS` updates status.
  - Assert `ESCALATE` leaves pending.
  - Assert failure to parse/error leaves pending.

---

## 6. Tasks

- [x] 1. Create this spec file.
- [ ] 2. Register `DA_AUTO_ADJUDICATE` flag in `core/flags.py` and update `CLAUDE.md`.
- [ ] 3. Update `storage/db.py` to support `auto_dismissed` status.
- [ ] 4. Create `inference/adjudicator.py` (`LocalAdjudicator` logic).
- [ ] 5. Hook `LocalAdjudicator` into `core/proactive_scheduler.py`.
- [ ] 6. Write unit tests in `tests/test_local_adjudicator.py`.
