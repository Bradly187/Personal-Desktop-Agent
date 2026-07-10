# Tasks — B2: Fix `CRITIC_REJECT` crash path

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b2-critic-reject/requirements.md`
> File: `inference/executors/plan_executor.py`, lines 819-828

---

## Tasks

- [x] 1. **Fix `AgentResult` construction** — at `plan_executor.py:825`, supply `model_used`.
  - Change:
    ```python
    critic_res = AgentResult(goal=goal, domain="plan", success=False, error=new_steps[0].body)
    ```
  - To:
    ```python
    model_str = getattr(new_steps[0], "model", None) or "critic"
    critic_res = AgentResult(
        goal=goal, domain="plan", success=False,
        error=new_steps[0].body,
        model_used=model_str,
    )
    ```
  - Satisfies R1.1, R1.2

- [x] 2. **Remove `agent._observations.record(...)`** — at `plan_executor.py:826`, replace the undefined attribute call with a structured warning log.
  - Change:
    ```python
    agent._observations.record(rejection_step, critic_res)
    ```
  - To:
    ```python
    log.warning("Critic rejected replan for goal=%r: %s", goal, new_steps[0].body)
    ```
  - Satisfies R2.1, R2.3, R2.4

- [x] 3. **Add `try/except` guard** — wrap the `CRITIC_REJECT` block (lines 819-828) so any unexpected exception halts safely.
  - Wrap in `try: ... except Exception as exc: log.error(...); return []`
  - Satisfies R3.1

- [x] 4. **Add unit tests** — `tests/test_plan_executor.py` (new cases):
  - Mock critic returning `CRITIC_REJECT` step; assert no `TypeError` raised (R1.2)
  - Assert no `AttributeError` on `_observations` (R2.1)
  - Assert return value is `[]` on rejection (R3.1)
  - Assert log warning emitted with goal and rejection body (R2.3)

- [x] 5. **Run full test suite** — `pytest -x`; confirm green.
