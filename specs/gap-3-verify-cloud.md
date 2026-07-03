# Spec: GAP-3: Cross-model verify judge for workflow fan-out

---

## 1. Background — the "Why"

`_maybe_verify` judges worker outputs with the same resident model that produced them. Correlated blind spots pass verification.

**Status:** Shipped (PR #___)
**Approved:** Brad, 2026-07-03

---

## 2. Glossary

- **Verify Judge**: The step in the workflow runner that evaluates whether a worker output satisfies a given criterion.
- **Workflow Fan-out**: The process where a goal is decomposed and distributed to multiple workers.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Cloud Verify Judge

**User Story:** As Brad, I want the workflow verify judge to use a different model than the generator so that correlated blind spots are avoided.

#### Acceptance Criteria
1. WHEN `DA_WORKFLOW_VERIFY_CLOUD` is ON, THE `_maybe_verify` judge SHALL use the Bedrock cloud backend if available.
2. IF the cloud backend fails or is disabled, THEN THE judge SHALL fall back to the local model.
3. THE judge SHALL preserve the fail-safe behavior where any error results in `verified=False`.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/workflow.py` verify path.

### Configuration (flat YAML)

```yaml
gap-3-verify-cloud:
  enabled: false          # DA_WORKFLOW_VERIFY_CLOUD
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - Test cloud error ⇒ not-verified.
  - Test flag OFF ⇒ zero cloud calls.

---

## 6. Tasks

- [x] 1. Add `DA_WORKFLOW_VERIFY_CLOUD` to `core/flags.py`.
- [ ] 2. Update `_maybe_verify` in `inference/workflow.py`.
