# Spec: Self-Skilling — experience-driven macro & skill proposal

---

## 1. Background — the "Why"

Today PDA learns from experience only at the *parameter* layer: the
`self_evolution_candidates` staging table + `ContinuousTrainer` promote learned
vocabulary/routing weights into the domain overlay, eval-gated
(`tests/test_evolution_candidates.py`, `adaptive/continuous_trainer.py`). It does
**not** learn new *capabilities*. Yet two kinds of experience recur for a single
user: (a) the same multi-step plan executed by hand over and over (a latent
**macro**), and (b) repeated `pipeline_failure` with no tool that fits the intent
(a latent **capability gap**). The skill architecture is unusually ready for this
— adding a skill is a manifest + a FastMCP server file with **zero edits** to
`command_executor` or any prompt (`skills/registry.py`) — so the agent can do the
boring 90% (notice, scaffold, test) and stop at a human approval gate. This keeps
PDA's strongest differentiator intact: a hardened, predictable, single-user safety
posture (`AGENTS.md` #4, #6, #7).

Related: `../dev-agent-critic/` (Critic+Tester reused as the codegen gate),
`../edit-format-aci/` (lint gate reused pre-write), the existing
`self_evolution_candidates` lifecycle.

**Status:** Draft
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Macro (rung 2)**: a named, replayable sequence composed *only* of
  already-vetted verbs/skill-tools. New behavior, **no new code, no new egress**.
- **Skill proposal (rung 3)**: a drafted FastMCP server + manifest + generated
  pytest, staged for human approval. Greenfield code, gated, never auto-enabled.
- **Capability gap**: a recurring intent for which no existing verb or skill tool
  fits — detected from repeated `pipeline_failure` episodes with no matching tool.
- **MacroDetector / GapDetector**: offline miners over episodic memory + the
  escalation/failure record that emit candidates.
- **SkillProposer**: the DevAgent path that scaffolds a rung-3 draft, routed
  through the existing lint → Critic → Tester gates.
- **`self_evolution_candidates`**: existing staging table
  (`kind`, `text`, `action_or_wrong`, `domain`, `reason`, `source_refs`, `status`,
  `eval_delta`, `decided_ts`; `UNIQUE(kind,text,action_or_wrong)`; statuses
  `proposed → promoted | rejected`). This spec adds two `kind` values:
  `"macro"` and `"skill_proposal"`. **No schema change** — reuse columns.
- **Approval chip**: the existing `spawn_task` / chip surface used to put a
  proposal in front of Brad; `~/.claude/skills/enabled.json` is the user-state
  toggle that `start_skill()` honors for hot-load without restart.

### Non-goals (explicit)

- **Rung 4 — autonomous authoring + auto-enable + auto-egress is OUT OF SCOPE and
  forbidden by this spec.** No self-authored skill is ever enabled, granted
  credentials, or permitted to egress without an explicit human approval. The
  agent proposes; the human admits. This is the `fail-safe-DENY` philosophy
  (`AGENTS.md` #4) applied to capability acquisition.
- No self-provisioning of API credentials or secrets, ever.
- No modification of `command_executor` verbs or core prompts (the skill model
  exists precisely so this is unnecessary).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Macro detection (rung 2)

**User Story:** As Brad, I want the agent to notice when I keep doing the same
multi-step thing and offer to save it as one command, so repetitive workflows
become a single utterance.

#### Acceptance Criteria
1. THE `MacroDetector` SHALL run **offline** (never on the 60 Hz loop nor inside
   any request path) over episodic memory, in a supervised background task.
   <!-- AGENTS.md #2: no heavy work in the tick loop. -->
2. WHEN ≥ `min_occurrences` near-identical successful multi-step plans are found,
   THE `MacroDetector` SHALL stage one candidate `kind="macro"` with
   `source_refs` listing the contributing episode ids.
3. THE staged macro SHALL reference **only** verbs/skill-tools that currently
   exist and are enabled; IF any referenced tool is absent, THEN THE candidate
   SHALL NOT be staged.
4. THE macro insert SHALL be idempotent on `UNIQUE(kind,text,action_or_wrong)`
   (re-detecting the same macro returns the existing row).
5. WHILE a flare is active, THE `MacroDetector` SHALL skip its run (pain-day hook).

### Requirement 2: Capability-gap detection (rung 3 trigger)

**User Story:** As Brad, I want the agent to recognize when it repeatedly fails for
lack of a tool, so it can propose building one instead of failing silently again.

#### Acceptance Criteria
1. THE `GapDetector` SHALL run offline over the failure/escalation record and
   identify recurring intents with `reason="pipeline_failure"` and **no** matching
   verb or skill tool (checked via `SkillRegistry` + the verb set).
2. WHEN a gap recurs ≥ `min_occurrences` times, THE `GapDetector` SHALL stage one
   candidate `kind="skill_proposal"` (status `proposed`) with `source_refs` to the
   failing episodes and a natural-language capability description in `text`.
3. IF the gap can be satisfied by composing existing tools, THEN THE detector
   SHALL prefer a `kind="macro"` candidate over `skill_proposal` (no new code when
   composition suffices).
4. IF a candidate for the same capability already exists in status `proposed` or
   `rejected`, THEN THE detector SHALL NOT re-stage it (respect a prior human
   "no").

### Requirement 3: Skill drafting (rung 3, gated codegen)

**User Story:** As Brad, I want a proposed skill to arrive fully scaffolded and
self-tested, so my only job is to review and approve.

#### Acceptance Criteria
1. WHEN a `skill_proposal` candidate is selected for drafting, THE `SkillProposer`
   SHALL generate a FastMCP server file, a manifest (`enabled: false`), and a
   pytest, written **only** within the skills scope
   (`skills/servers/`, `skills/manifests/`, `tests/`).
   <!-- AGENTS.md #7: respect writable_roots; never write outside scope. -->
2. THE generated server SHALL pass the existing pre-write lint gate
   (`inference/edit_format.py` `ast.parse`, fail-closed) BEFORE any file is
   written; IF it fails, THEN no file SHALL be written and the failure SHALL be
   recorded on the candidate.
3. THE generated diff SHALL pass the independent Critic (`inference/critic.py`);
   IF the Critic returns REVISE/BLOCK, THEN drafting SHALL stop and surface the
   diagnostic (no partial skill left enabled).
4. THE generated pytest SHALL run one-shot in the sandbox
   (`inference/tester.py` → `inference/sandbox.run_sandboxed`); a failing test
   SHALL be surfaced as an observation on the candidate, NOT auto-rolled-back and
   NOT auto-approved.
5. THE drafted manifest SHALL declare every external-effect tool in `send_tools`
   so it is gated by default; IF tool effect is unknown, THEN it SHALL be treated
   as a send (`is_send_tool` fail-safe).

### Requirement 4: Human approval gate (the rung-4 firewall)

**User Story:** As Brad, I want nothing the agent invents to run until I say so,
so self-skilling can never widen my attack surface behind my back.

#### Acceptance Criteria
1. THE system SHALL NEVER set a self-authored skill `enabled: true` without an
   explicit human approval recorded against the candidate.
2. WHEN a draft is ready, THE system SHALL surface it via an approval chip
   (`spawn_task`) summarizing the capability, the source episodes, and the
   generated test outcome — and SHALL take no enabling action on silence/timeout.
   <!-- AGENTS.md #4: fail-safe to DENY on silence. -->
3. IF approved, THEN the system SHALL write the enabled-state to
   `~/.claude/skills/enabled.json` (user state, never the checked-in manifest) and
   hot-load via `SkillRegistry.start_skill()`; the candidate moves to `promoted`.
4. IF rejected (or on timeout), THEN the candidate SHALL move to `rejected` and
   SHALL NOT be re-proposed (R2.4).
5. THE system SHALL NEVER self-provision a credential or secret for a drafted
   skill; a skill needing credentials SHALL surface that requirement in the chip
   for Brad to satisfy manually.
6. FOR ALL drafted skills, the inbound-taint / `ContentFilter` scrub / send-gate
   security flow in `DevAgent._execute_skill_step` SHALL apply unchanged — a
   self-authored skill gets exactly the same runtime treatment as a human-authored
   one, never less.

### Requirement 5: Macro promotion (rung 2 enablement)

#### Acceptance Criteria
1. WHEN a `kind="macro"` candidate is approved, THE system SHALL register it as a
   replayable named workflow routable like any other intent (keyword + the planned
   sequence), with `status=promoted`.
2. THE macro SHALL execute by dispatching its existing constituent verbs/tools
   through the normal `CommandExecutor` path — it SHALL NOT introduce a new
   execution mechanism or bypass any existing gate.
3. IF any constituent tool has since been disabled/removed at replay time, THEN
   the macro SHALL fail safe with a CLARIFY rather than executing a partial
   sequence.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** two offline supervised background miners
  (`MacroDetector`, `GapDetector`) cloned from the `ProactiveScheduler` /
  `ResourceGovernor` supervised-loop pattern — **never** the tick loop. Drafting
  reuses the DevAgent WRITE_FILE path (lint → Critic → Tester) with scope pinned
  to `skills/`. Approval reuses the chip (`spawn_task`) + `enabled.json` +
  `SkillRegistry.start_skill()` hot-load.
- **New `Command` fields:** none. Macros/proposals live in the DB, not on the
  `Command` DTO.
- **Models / VRAM:** drafting runs on the already-loaded plan/general model with a
  fresh reviewer context for the Critic (no new VRAM — `AGENTS.md` #6, mirrors the
  Critic precedent). Detectors are non-LLM (pattern/frequency mining) where
  possible; any LLM summarization of a gap is offline and best-effort.
- **Persistence:** **no schema change.** Reuse `self_evolution_candidates` with
  two new `kind` values (`"macro"`, `"skill_proposal"`); `source_refs` carries the
  motivating episode/escalation ids; `eval_delta` carries the generated-test
  outcome for proposals. `storage/db.py` stays the schema source of truth
  (`AGENTS.md` #1); `PRAGMA user_version` is **unchanged**.
- **Cross-platform:** none — does not touch the bridge. (The approval chip is a
  desktop/CLI surface; if a future iPad approval banner is added, mirror the JSON
  per `AGENTS.md` #3 in a follow-up.)

### Configuration (flat YAML)

```yaml
self_skilling:
  enabled: false              # ship behind a flag; default off until evals pass
  macro:
    min_occurrences: 4        # near-identical plans before a macro is staged
    similarity: 0.9           # plan-shape match threshold
  gap:
    min_occurrences: 3        # recurring no-tool failures before a proposal
  draft:
    writable_roots:           # AGENTS.md #7 — drafting is pinned to skills scope
      - skills/servers
      - skills/manifests
      - tests
  approval:
    auto_enable: false        # MUST stay false — rung-4 firewall (R4.1)
    auto_provision_credentials: false   # MUST stay false (R4.5)
```

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite:** add a `self_skilling` suite — `evals/suites/self_skilling.jsonl`
  with (a) macro-detection fixtures (synthetic episode runs → expected staged
  macro), (b) gap-vs-macro arbitration cases (R2.3), (c) a draft-gate case where a
  deliberately broken generated server must be rejected pre-write (R3.2). Lock the
  baseline in `evals/baselines/self_skilling.json`.
- **Unit/integration tests:** `tests/test_self_skilling.py`, one assertion per
  numbered criterion (cite the number in the test name). Reuse the existing
  `self_evolution_candidates` test helpers. Must include:
  - R4.1/R4.2: no enable on silence/timeout (fail-safe-DENY).
  - R4.6: a drafted skill goes through the identical taint/scrub/send-gate path.
  - R3.4: a failing generated test surfaces as observation, never auto-approve.

---

## 6. Tasks

- [ ] 1. `MacroDetector` offline miner + macro staging — satisfies R1.1–R1.5
- [ ] 2. `GapDetector` offline miner + gap/macro arbitration — satisfies R2.1–R2.4
- [ ] 3. `SkillProposer` drafting through lint→Critic→Tester, scope-pinned to
      `skills/` — satisfies R3.1–R3.5
- [ ] 4. Approval surface: chip + `enabled.json` write + `start_skill()` hot-load;
      reject/timeout → `rejected` — satisfies R4.1–R4.6
- [ ] 5. Macro promotion + safe replay through `CommandExecutor` — satisfies R5.1–R5.3
- [ ] 6. Eval suite + `tests/test_self_skilling.py` (one per criterion) — §5
- [ ] 7. Update `CLAUDE.md` (Known Gotchas: new flag `self_skilling`, default OFF)
      + `docs/file-map.md` if new modules land
- [ ] 8. (Sequencing) Ship rung 2 (tasks 1, 5) first; gate rung 3 (tasks 2–4)
      behind the rung-2 baseline holding in production
