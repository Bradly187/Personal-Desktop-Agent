# Spec: Fix `CRITIC_REJECT` crash path — B2

**Status:** In Progress
**Approved:** Brad, 2026-07-09
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

The `DA_REPLAN_CRITIC` feature (spec: `specs/dev-agent-critic/`) allows the
critic model to reject an entire proposed recovery plan by returning a
`CRITIC_REJECT` action. The handler at
`inference/executors/plan_executor.py:819-828` has two bugs that are **latent
only because `DA_REPLAN_CRITIC` defaults OFF** in `core/flags.py`:

1. **Missing `model_used` field** (line 825): `AgentResult` is constructed
   without `model_used`, but `inference/plan_parser.py:103` declares it as a
   non-optional `str` field with no default. This raises `TypeError` when Python
   tries to construct the dataclass.
2. **Undefined `_observations` attribute** (line 826): `agent._observations` is
   called once in this branch and is **defined nowhere in the repo** — no
   `__init__` assignment, no property, no `__getattr__` delegation that resolves
   to it. Python's two-hop `__getattr__` walk through the satellite delegates
   (`StepExecutor`, `SagaManager`, `ContextBuilder`) finds nothing and raises
   `AttributeError`. Because `_try_replan` is called without a surrounding
   try/except, the exception kills the entire plan.

Either bug kills the plan loop on the first critic rejection. The feature was
shipped dark-broken; this spec fixes it before anyone flips `DA_REPLAN_CRITIC`
ON.

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B2;
spec `specs/dev-agent-critic/`.

---

## 2. Glossary

- **`AgentResult`**: Dataclass in `inference/plan_parser.py` returned from
  planner/executor calls. Field `model_used: str` has no default.
- **`CRITIC_REJECT`**: An `AgentStep` action value returned by the critic when
  it rejects an entire recovery plan (as opposed to requesting revision of a
  single step).
- **`_try_replan` / `try_replan_after_critic`**: Function in
  `inference/executors/plan_executor.py` that handles the post-critic replan
  loop; the buggy handler is at lines 819-828.
- **`DA_REPLAN_CRITIC`**: Feature flag; currently defaults `"0"` in
  `core/flags.py`. The bugs are latent while this is OFF.
- **`_observations`**: Undefined attribute referenced at `plan_executor.py:826`.
  The intent appears to be recording an observation for the trajectory/memory
  layer, but no such attribute exists on `DevAgent` or any of its satellites.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: `CRITIC_REJECT` branch constructs `AgentResult` correctly

**User Story:** As Brad, I want the critic rejection path to fail gracefully, so
that enabling `DA_REPLAN_CRITIC` doesn't immediately crash the plan loop.

#### Acceptance Criteria
1. WHEN the critic returns `CRITIC_REJECT`, THE `plan_executor` SHALL construct
   `AgentResult` with a non-empty `model_used` string that identifies the critic
   model (e.g. `"critic"` or the actual model identifier from the critic result).
2. THE `AgentResult` SHALL be constructable without raising `TypeError` — i.e.,
   all required fields are supplied.

### Requirement 2: Undefined `_observations` reference removed or resolved

#### Acceptance Criteria
1. THE `plan_executor.py:826` call to `agent._observations.record(...)` SHALL
   be removed or replaced with a valid, defined call.
2. IF the intent was to log the rejection to the trajectory, THEN the
   replacement SHALL use the existing trajectory/logging API that `DevAgent` or
   `StepExecutor` exposes (e.g. `agent._trajectory.append(...)` or equivalent
   defined attribute).
3. IF no suitable recording API exists, THEN the call SHALL be replaced with a
   structured `log.warning(...)` so the rejection is observable without crashing.
4. THE fix SHALL not introduce any new undefined attribute access.

### Requirement 3: `_try_replan` / `try_replan_after_critic` wrapped against exceptions

#### Acceptance Criteria
1. WHEN the `CRITIC_REJECT` handler raises an unexpected exception, THE plan
   loop SHALL log the error and return an empty step list (safe halt), not
   propagate the exception up to the caller.
2. THE fix SHALL be minimal — no restructuring of the surrounding replan loop.

---

## 4. Technical Design

**File:** `inference/executors/plan_executor.py`, lines 819-828.

**Change 1 — `model_used`:** Supply a sentinel string. The critic result
object that caused the rejection is available in context; use its `.model`
field if present, otherwise fall back to the literal string `"critic"`:

```python
model_str = getattr(new_steps[0], "model", None) or "critic"
critic_res = AgentResult(
    goal=goal, domain="plan", success=False,
    error=new_steps[0].body,
    model_used=model_str,
)
```

**Change 2 — `_observations.record`:** Replace with `log.warning(...)`:

```python
log.warning("Critic rejected replan: %s", new_steps[0].body)
```

(If a trajectory append API is confirmed to exist at review time, prefer that.)

**Change 3 — guard:** Wrap the `CRITIC_REJECT` block in a `try/except Exception`
that logs and returns `[]`.

- **No schema changes.** No VRAM impact. No Swift bridge changes.
- **Flag:** `DA_REPLAN_CRITIC` remains OFF by default; this spec only makes the
  ON path safe to enable.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_plan_executor.py` (add cases):
  - R1.1: mock critic returning `CRITIC_REJECT`; assert no `TypeError`.
  - R2.1: assert no `AttributeError` on `_observations`.
  - R3.1: assert the plan loop returns `[]` on rejection, not raises.
- **Manual:** Set `DA_REPLAN_CRITIC=1` in env, run a plan that the critic would
  reject; verify graceful halt with a log warning, no crash.

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Fix `AgentResult` construction — add `model_used` from critic result — satisfies R1.1, R1.2
- [ ] 2. Replace `agent._observations.record(...)` with `log.warning(...)` — satisfies R2.1, R2.3, R2.4
- [ ] 3. Add `try/except` guard around the `CRITIC_REJECT` block — satisfies R3.1
- [ ] 4. Add unit tests — satisfies R1.1, R2.1, R3.1
- [ ] 5. Run full test suite; confirm green
