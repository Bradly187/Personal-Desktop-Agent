# Spec: Align `DA_REPO_CONTEXT` / `DA_DELEGATE` flag defaults — B3

**Status:** In Progress
**Approved:** Brad, 2026-07-09 — Path B (accept ON, align docs + lock evals)
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

Two DevAgent feature flags are documented and registered as **OFF by default**,
but their `os.environ.get(...)` calls in `inference/dev_agent.py` use `"1"` as
the default argument, making them **ON whenever the env-var is unset**:

| Flag | `flags.py` default | `dev_agent.py` default | Comment in code |
|---|---|---|---|
| `DA_REPO_CONTEXT` | `"0"` (OFF) | `"1"` (ON) | "Default OFF until eval baseline locks" |
| `DA_DELEGATE` | `"0"` (OFF) | `"1"` (ON) | "Default OFF until eval baseline locks" |

Both features were spec'd to wait behind an eval-baseline-lock gate before
going live (specs `specs/repo-context-ingestion/` and
`specs/dev-agent-delegate-verb/`). With env unset the gate never fires —
both features are silently active in production. This can cause:
- `DA_REPO_CONTEXT`: extra tokens injected into every LLM plan prompt, which
  the baseline-lock eval may flag as a regression if run now.
- `DA_DELEGATE`: planner vocabulary includes the `[DELEGATE q]` verb, which
  spins sub-agents the user never authorised activating.

The fix is to align `dev_agent.py` defaults with `flags.py`. If a deliberate
decision was made to turn these ON, then `flags.py`, the in-code comments, and
`CLAUDE.md` must be updated to match (end the silent drift either way).

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B3.

---

## 2. Glossary

- **`DA_REPO_CONTEXT`**: Flag enabling stable repo-facts block injected ahead of
  RAG context in DevAgent plan prompts. Spec: `specs/repo-context-ingestion/`.
- **`DA_DELEGATE`**: Flag enabling the `[DELEGATE q]` planner verb that spins a
  bounded read-only investigation sub-agent. Spec: `specs/dev-agent-delegate-verb/`.
- **`flags.py`**: `core/flags.py` — the single registry of feature flags and
  their documented defaults. AGENTS.md §1 analogue for flags.
- **`_repo_context_enabled` / `_delegate_enabled`**: Boolean instance attributes
  on `DevAgent` set from the env-var at `__init__` time.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Flag defaults consistent across all three sources of truth

**User Story:** As Brad, I want feature flags to be OFF when the env-var is
unset (unless I've deliberately turned them ON and documented that), so that I
don't accidentally run experimental features in production.

#### Acceptance Criteria
1. THE `os.environ.get("DA_REPO_CONTEXT", ...)` call in `dev_agent.py` SHALL
   use `"0"` as its default argument, matching `flags.py:60`.
2. THE `os.environ.get("DA_DELEGATE", ...)` call in `dev_agent.py` SHALL use
   `"0"` as its default argument, matching `flags.py:61`.
3. WHEN both env-vars are unset, THE `DevAgent.__init__` SHALL set
   `self._repo_context_enabled = False` and `self._delegate_enabled = False`.
4. THE inline comments in `dev_agent.py` that say "Default OFF" SHALL remain
   accurate after the fix.
5. `CLAUDE.md` Feature Flags table SHALL reflect the correct default (OFF) for
   both flags.

### Requirement 2: Deliberate ON path (alternative resolution)

> [!IMPORTANT]
> If Brad has intentionally decided both features should be ON, this spec's
> resolution is to flip `flags.py` defaults and update CLAUDE.md to match —
> **not** to flip them OFF. Either way the three sources must agree.

#### Acceptance Criteria
1. IF the decision is to keep both flags ON, THEN `flags.py:60-61` SHALL be
   updated to `"1"` and the in-code comments SHALL be updated to "Default ON".
2. IF the decision is to keep both flags ON, THEN the eval baseline locks for
   both features SHALL be run and recorded before this spec is closed.
3. FOR ALL flag registrations in `flags.py`, THE default in `flags.py` SHALL
   match the `os.environ.get(...)` default used in the consuming module.

---

## 4. Technical Design

**Decision needed from Brad first:** keep ON or flip to OFF?

**Path A — flip to OFF (matches documented intent):**
- `dev_agent.py:292`: change `"1"` → `"0"` in `os.environ.get("DA_REPO_CONTEXT", "1")`
- `dev_agent.py:307`: change `"1"` → `"0"` in `os.environ.get("DA_DELEGATE", "1")`
- Update `CLAUDE.md` Feature Flags table rows for both flags to show default OFF.

**Path B — accept as ON (consciously):**
- `flags.py:60-61`: change `"0"` → `"1"` for both entries.
- Update inline comments in `dev_agent.py` from "Default OFF" to "Default ON".
- Update `CLAUDE.md` Feature Flags table.
- Run `evals/` baseline lock for `repo-context-ingestion` and `dev-agent-delegate-verb` suites.

No schema changes. No VRAM impact. No Swift bridge changes.

> [!IMPORTANT]
> **Open question for Brad:** Which resolution — Path A (flip dev_agent.py to
> match flags.py, features OFF) or Path B (accept features ON and lock baselines)?
> This spec cannot be promoted to `In Progress` without an answer.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_dev_agent_flags.py` (or extend existing):
  - R1.1-R1.3: instantiate `DevAgent` with env-vars unset; assert both booleans are `False`.
  - R1.1-R1.3: instantiate with `DA_REPO_CONTEXT=1`; assert `_repo_context_enabled = True`.
- **If Path B:** run `evals/` baseline lock for affected suites.

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Brad confirms Path A or Path B (open question above)
- [ ] 2A (if Path A). Change `dev_agent.py:292` default `"1"` → `"0"` — satisfies R1.1
- [ ] 2B (if Path A). Change `dev_agent.py:307` default `"1"` → `"0"` — satisfies R1.2
- [ ] 3. Update `CLAUDE.md` Feature Flags table — satisfies R1.5
- [ ] 4. Add / extend unit tests — satisfies R1.3
- [ ] 5. (Path B only) Run eval baseline locks for affected suites
- [ ] 6. Run full test suite; confirm green
