# Spec: GAP-2: Independent review of recovery plans (replan critic)

---

## 1. Background — the "Why"

`_replan` output executes without any independent check. First plans get the two-gate human approval; recovery plans (generated *after* something already went wrong — precisely when context is most likely poisoned) do not get a Critic pass.

**Status:** Shipped (PR #___)
**Approved:** Brad, 2026-07-03

---

## 2. Glossary

- **Recovery Plan**: A new plan generated via `_replan` when execution of a previous step fails.
- **Critic**: The `inference/critic.py` reviewer that evaluates the plan or diff.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Replan Critic

**User Story:** As Brad, I want recovery plans to be reviewed by the critic so that poisoned context doesn't lead to cascading failures.

#### Acceptance Criteria
1. WHEN `DA_REPLAN_CRITIC` is ON, THE `_replan` process SHALL run a bounded critic-style check over the new plan.
2. WHEN the critic returns `REVISE`, THE `_replan` process SHALL consume the existing replan budget.
3. THE process SHALL NOT let a `REVISE` verdict trigger a saga rollback.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/dev_agent.py` (`_replan`).

### Configuration (flat YAML)

```yaml
gap-2-replan-critic:
  enabled: false          # DA_REPLAN_CRITIC
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - Test that OFF ⇒ replan path byte-identical.
  - Test that ON ⇒ REVISE verdict decrements replan budget.

---

## 6. Tasks

- [x] 1. Add `DA_REPLAN_CRITIC` to `core/flags.py`.
- [ ] 2. Update `_replan` in `inference/dev_agent.py`.
