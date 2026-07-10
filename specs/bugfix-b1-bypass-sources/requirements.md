# Spec: Unify `_BYPASS_SOURCES` — B1 routing divergence fix

**Status:** In Progress
**Approved:** Brad, 2026-07-09
**Owner / author session:** Antigravity (2026-07-09)

## 1. Background — the "Why"

`_BYPASS_SOURCES` is a set/tuple that tells the routing layer which command
sources should skip LLM gate processing (e.g. verb de-glue, DevAgent pre-gate).
It is currently defined **twice with diverging values**:

- `core/hybrid_coordinator.py:227` → `{"touch", "multimodal"}`
- `core/event_dispatcher.py:20` → `("touch", "multi")`

`FusionEngine` emits `source="multimodal"` for voice-click bypass commands
(`core/fusion_engine.py:663`). The string `"multi"` is emitted **nowhere** in
the codebase. Consequence: `route_impl` (`event_dispatcher.py:37, 63`) does not
recognise `"multimodal"` as a bypass source — the command passes through
`deglue_command_verb` and the DevAgent pre-gate, directly contradicting the
documented invariant that bypass sources skip LLM interference. This is a live
routing correctness bug on every voice-click. Severity: **CRITICAL**.

Related: audit report `docs/audits/2026-07-09-oop-antipattern-audit.md` §B1.
The split-brain defect class was described by the 07-06 repair memo as already
resolved; this audit confirms it reappeared.

---

## 2. Glossary

- **`_BYPASS_SOURCES`**: The collection of `Command.source` values whose commands
  skip verb de-glue and the dev-agent/LLM pre-gate because the action is already
  fully resolved before reaching the coordinator.
- **`EventDispatcher.route_impl`**: The main routing method in
  `core/event_dispatcher.py` that checks `cmd.source in _BYPASS_SOURCES`.
- **`HybridCoordinator`**: Parent coordinator in `core/hybrid_coordinator.py`;
  currently defines its own copy of the constant.
- **`FusionEngine`**: Sensor fusion loop in `core/fusion_engine.py`; the source
  of `source="multimodal"` commands.
- **Leaf module**: A module with no runtime imports from the rest of the `core`
  package — safe to import without pulling the full coordinator graph.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Single authoritative constant

**User Story:** As Brad, I want voice-click commands to bypass LLM processing,
so that "click" spoken while my cursor is positioned executes immediately with
no LLM round-trip or dev-agent interference.

#### Acceptance Criteria
1. THE codebase SHALL contain exactly **one** definition of `_BYPASS_SOURCES`.
2. THE single definition SHALL be in a leaf module (`core/routing_constants.py`
   or equivalent) that carries no runtime dependency on `HybridCoordinator`,
   `EventDispatcher`, or `FusionEngine`.
3. BOTH `event_dispatcher.py` and `hybrid_coordinator.py` SHALL import
   `_BYPASS_SOURCES` from the single leaf module (no local redefinition).
4. THE value SHALL be `frozenset({"touch", "multimodal"})` — matching what
   `FusionEngine` actually emits and what the documented invariant requires.
5. WHEN `FusionEngine` emits `source="multimodal"`, THE `route_impl` SHALL
   recognise it as a bypass source and skip both `deglue_command_verb` (ed:38)
   and the dev-agent pre-gate (ed:63).
6. IF a future module needs to check bypass membership, THEN it SHALL import
   from the leaf module — not define a local copy.

### Requirement 2: No regression on touch bypass

#### Acceptance Criteria
1. WHEN `cmd.source == "touch"`, THE `route_impl` SHALL continue to skip
   de-glue and the dev-agent pre-gate, identical to current behaviour.
2. THE routing eval suite (`evals/suites/routing.jsonl` or equivalent) SHALL
   include a case asserting `source="multimodal"` is treated as bypass.
3. THE routing eval suite SHALL include a case asserting `source="touch"` is
   treated as bypass.

---

## 4. Technical Design

- **New leaf module:** `core/routing_constants.py`
  - Contains: `_BYPASS_SOURCES: frozenset[str] = frozenset({"touch", "multimodal"})`
  - Contains: `_SKIP_GATE1_SOURCES: frozenset[str] = frozenset({"voice_local"})` (move from `hybrid_coordinator.py:228` at the same time to keep the seam clean)
  - No imports from `core`, `inference`, or `storage` — pure constants.
- **`core/event_dispatcher.py`:** Remove local `_BYPASS_SOURCES = ("touch", "multi")`. Add `from core.routing_constants import _BYPASS_SOURCES`.
- **`core/hybrid_coordinator.py`:** Remove local `_BYPASS_SOURCES = {"touch", "multimodal"}` and `_SKIP_GATE1_SOURCES`. Add `from core.routing_constants import _BYPASS_SOURCES, _SKIP_GATE1_SOURCES`.
- **No schema changes.** No VRAM impact. No Swift bridge changes.
- **Import cycles:** `routing_constants.py` is a pure-constants leaf; no new cycle risk.

---

## 5. Behavior Verification

- **Unit test:** `tests/test_routing_constants.py`
  - R1.1: assert exactly one module defines `_BYPASS_SOURCES` (grep + import check).
  - R1.4: assert `"multimodal" in _BYPASS_SOURCES` and `"multi" not in _BYPASS_SOURCES`.
  - R1.5/R2.1: mock `route_impl`; assert `source="multimodal"` skips de-glue.
  - R2.2/R2.3: assert `source="touch"` skips de-glue.
- **Eval case:** add to `evals/suites/routing.jsonl` — `source=multimodal` → bypass (no LLM call emitted).

---

## 6. Tasks

> **Gate 2:** Draft `tasks.md` and present it for explicit approval before executing.

- [ ] 1. Create `core/routing_constants.py` with `_BYPASS_SOURCES` and `_SKIP_GATE1_SOURCES` — satisfies R1.1, R1.2, R1.4
- [ ] 2. Update `event_dispatcher.py`: remove local tuple, import from leaf — satisfies R1.3, R1.5
- [ ] 3. Update `hybrid_coordinator.py`: remove local set, import from leaf — satisfies R1.3
- [ ] 4. Add unit tests (`tests/test_routing_constants.py`) — satisfies R1.1, R1.4, R1.5, R2.1
- [ ] 5. Add eval cases to routing suite — satisfies R2.2, R2.3
- [ ] 6. Run full test suite; confirm green
