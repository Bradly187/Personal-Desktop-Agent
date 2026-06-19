# Spec: <Feature Name>

> Copy this file to `specs/<feature-slug>/requirements.md` (and split Design/Tasks
> into sibling `design.md` / `tasks.md` as the spec grows). One feature → one folder.
> Specs are the **source of truth**; code is the build artifact. Keep this updated
> as the design evolves — a stale spec is worse than no spec.

---

## 1. Background — the "Why"

<2–4 sentences. What problem does this solve, for whom, and why now? For this
project, tie it to an accessibility need or a concrete user (Brad) workflow where
relevant. Link related specs with relative paths, e.g. `../behavioral-twin-state/`.>

**Status:** Draft | In Progress | Shipped (PR #___) | Superseded by `<spec>`
**Owner / author session:** <Antigravity | Claude Code | …>

---

## 2. Glossary

<Define every proper-noun component, DTO, and store the spec references, so an
agent reading cold knows exactly what each name binds to. Mirror the style of
`behavioral-twin-state/requirements.md`.>

- **<ComponentName>**: <one-line definition>

---

## 3. Requirements (EARS acceptance criteria)

> Use **EARS** notation — it is this repo's standard and is directly testable.
> Keywords: `THE <entity> SHALL <action>` (ubiquitous), `WHEN <trigger>, THE …`
> (event), `IF <condition>, THEN THE …` (unwanted/edge), `WHILE <state>, THE …`,
> `FOR ALL <set>, … SHALL …` (property). Number every criterion — tasks and tests
> reference them by number.

### Requirement 1: <Title>

**User Story:** As Brad, I want <capability>, so that <benefit>.

#### Acceptance Criteria
1. THE `<Component>` SHALL <observable behavior>.
2. WHEN <trigger>, THE `<Component>` SHALL <response> within <budget>.
3. IF <failure/ambiguity>, THEN THE `<Component>` SHALL <safe fallback>.
   <!-- Safe-by-default: on ambiguity/silence/timeout, fail to DENY or CLARIFY.
        See AGENTS.md #4. Destructive paths MUST route through a voice-approved
        / goal-session-gated pathway. -->

### Requirement 2: <Title>
…

---

## 4. Technical Design

> Lives here for a small feature; promote to `design.md` once it has diagrams or
> multiple components. State API contracts, the exact tools/models/versions, and
> where this hooks into the existing pipeline (FusionEngine / HybridCoordinator /
> CommandExecutor / DevAgent). Name the `Command` DTO fields you add — never pass
> raw dicts across boundaries.

- **Entry point / pipeline boundary:** <which gate / scheduler tier / verb>
- **New `Command` fields (if any):** <field: type — purpose>
- **Models / VRAM:** <model + GB; confirm `ResourceGovernor` eviction wiring per AGENTS.md #6>
- **Persistence:** <`agent.db` table(s); a schema change requires a migration +
  `PRAGMA user_version` bump — `storage/db.py` is the schema source of truth (AGENTS.md #1)>
- **Cross-platform (if it touches the bridge):** mirror the JSON payload in the
  Swift `WebSocketManager` (AGENTS.md #3).

### Configuration (flat YAML)

> Prefer one flat YAML block for deeply-nested config over prose tables — it costs
> fewer tokens to read and write, and is copy-paste-ready into a config file.

```yaml
feature_slug:
  enabled: false          # ship behind a flag; default off until evals pass
  threshold_ms: 600       # never hardcode interaction thresholds inline — route
                          # pain-day-sensitive values through BehavioralTwinState
                          # .apply_pain_day() (AGENTS.md #5)
  writable_roots:         # if WRITE_FILE/RUN_TERMINAL is involved, list scope
    - ~/Documents
```

---

## 5. Behavior Verification (executable, not prose)

> This repo already has an executable BDD layer — the `evals/` harness. Do NOT
> write static Gherkin `.feature` files; they drift and nothing runs them.
> Instead, add scenarios as suite lines so they gate regressions:

- **Eval suite:** add cases to `evals/suites/<suite>.jsonl`; lock the baseline in
  `evals/baselines/<suite>.json` (see `evals/README.md` and the
  `running-the-eval-harness` skill).
- **Unit/integration tests:** `tests/test_<feature>.py`, one assertion per
  numbered acceptance criterion above (cite the criterion number in the test name).

Each acceptance criterion in §3 SHOULD map to at least one eval case or test.

---

## 6. Tasks

> Promote to `tasks.md` once there is more than a handful. Reference acceptance
> criteria by number. Keep each task small enough to ship and verify independently.

- [ ] 1. <task> — satisfies R1.1, R1.2
- [ ] 2. <task> — satisfies R2.x
- [ ] 3. Add eval cases / tests for the above
- [ ] 4. Update `CLAUDE.md` Key Files / status header if the surface changed
