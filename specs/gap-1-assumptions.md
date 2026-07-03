# Spec: GAP-1: Assumption surfacing in the planner prompt

---

## 1. Background — the "Why"

Neither the planner prompt nor executor prompts ask the model to state assumptions or flag uncertainty. The only confidence signal in the whole pipeline is the critic confidence floor. A wrong premise ("I assume the schema has column X") enters the trajectory silently and conditions every later step.

**Status:** Shipped (PR #___)
**Approved:** Brad, 2026-07-03

---

## 2. Glossary

- **Planner Prompt**: The prompt provided to the dev agent planner model to generate execution plans.
- **Assumptions**: A list of implicit constraints or context the planner believes to be true while making the plan.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Assumption Surfacing

**User Story:** As Brad, I want to see the assumptions the planner is making, so that I can correct false premises before they cause failures.

#### Acceptance Criteria
1. THE `_PLAN_JSON_SCHEMA` SHALL include an optional `assumptions` array of strings.
2. WHEN `DA_PLAN_ASSUMPTIONS` is ON, THE planner prompt SHALL instruct the model to list assumptions.
3. WHEN `DA_PLAN_ASSUMPTIONS` is OFF, THE planner prompt SHALL be byte-identical to legacy behavior.
4. THE planner SHALL surface assumptions in the `DA_PLAN_PREVIEW` TTS voice preview if present.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/dev_agent.py` planner prompt assembly, `_PLAN_JSON_SCHEMA` in `inference/model_router.py`.
- **New `Command` fields (if any):** None.

### Configuration (flat YAML)

```yaml
gap-1-assumptions:
  enabled: false          # DA_PLAN_ASSUMPTIONS
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - Test that OFF ⇒ prompt unchanged.
  - Test that ON ⇒ schema accepts the field.

---

## 6. Tasks

- [x] 1. Add `DA_PLAN_ASSUMPTIONS` to `core/flags.py`.
- [ ] 2. Update `_PLAN_JSON_SCHEMA` in `inference/model_router.py`.
- [ ] 3. Update planner prompt assembly and preview in `inference/dev_agent.py`.
