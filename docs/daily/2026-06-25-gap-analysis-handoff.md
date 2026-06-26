# Handoff — Functionality Gap Analysis (2026-06-25)

> Snapshot of identified gaps between what's *built* and what's *wired / merged / tested*,
> produced from a full-codebase sweep on 2026-06-25. The codebase is mature and healthy
> (~2,337 tests, 14 locked eval baselines, disciplined "ship-dark" feature-flag pattern).
> These are **not bugs** — they are seams in delivery, coverage, and unfinished features.

## TL;DR — priority order

1. 🔴 **Commit conversation-mode work to its own branch** — data-loss risk (uncommitted, no branch).
2. 🔴 **WorkflowRunner has no live trigger** — orchestration layer is dead code.
3. 🟠 **Land the parity PRs** (#134 EDIT_FILE is the highest-value).
4. 🟠 **Backfill unit tests** for `model_router.py`, `db.py`, `dev_agent.py`.
5. 🟡 **Two unfinished features** (UDIFF edit format; FusionEngine scheduler wiring).
6. 🟡 **Eval baselines to confirm** (`rag_ablation` 0.2; `intent_satisfaction` unlocked).

---

## 🔴 Gap 1 — Conversation mode: unprotected uncommitted state

- **What:** `core/conversation_mode.py` + 40 tests (`tests/test_conversation_mode.py`) + `specs/conversation-mode/`,
  plus a live interception wired into `core/hybrid_coordinator._route_impl` (flag-gated OFF via
  `conversation_mode.enabled` in `~/.claude/ipad_bridge/config.json`).
- **Problem:** Exists only as **dirty working-tree state riding on `feat/workflow-orchestration`** —
  no branch, no commit. A branch switch/reset destroys it.
- **Status:** Built + tested + wired live (OFF by default; byte-identical legacy when unset).
  Fail-safe: any handler error returns `None` → falls through to ordinary command routing.
- **Action:** Commit to a dedicated branch (e.g. `feat/conversation-mode`) immediately, open PR.

## 🔴 Gap 2 — WorkflowRunner: built but no live trigger

- **What:** `inference/workflow.py` `WorkflowRunner.fan_out` — multi-agent fan-out + adversarial
  verify, journaled to the additive `agent_workflows` table. Shipped in PR #137.
- **Problem:** **Nothing calls it.** No import of `inference.workflow` / `WorkflowRunner` in
  `main.py`, `core/`, or `mcp_server/`. (Note: `scheduler.fan_out` in `core/scheduler.py` /
  `inference/dev_agent.py` is a *different* primitive — DevAgent's own sub-step fan-out — not this.)
- **Status:** "Tested building block, no live trigger" — acknowledged in the spec/PR body.
- **Action:** Wire the voice/MCP trigger + `pipeline` mode (the documented next step), or explicitly
  defer it in the PR so the capability isn't shipped unreachable.

## 🟠 Gap 3 — Four capability-parity PRs built but unmerged

All OPEN against `master`; MEMORY.md's "ALL 4 SHIPPED" framing is stale — they're built, not landed.

| PR | Branch | Adds | Value |
|----|--------|------|-------|
| [#134](https://github.com/Bradly187/Personal-Desktop-Agent/pull/134) | `feat/edit-file-verb` | `EDIT_FILE` surgical verb (aider-style SEARCH/REPLACE, fail-closed) | **Highest** — closes whole-file WRITE_FILE silent-elision risk |
| [#135](https://github.com/Bradly187/Personal-Desktop-Agent/pull/135) | `feat/first-class-search-tools` | `grep` / `glob_files` / `fetch_url` MCP tools | **MERGED to master** as of 2026-06-25 |
| [#136](https://github.com/Bradly187/Personal-Desktop-Agent/pull/136) | `feat/browser-ui-testing` | Playwright `preview_*` tools | **Conflicts resolved + pushed 2026-06-25** (now MERGEABLE) |
| [#137](https://github.com/Bradly187/Personal-Desktop-Agent/pull/137) | `feat/workflow-orchestration` | `WorkflowRunner` (see Gap 2) | Building block only |

- **Action:** Merge #134 next (real correctness win). #136 is ready to merge.

## 🟠 Gap 4 — Critical core modules with no dedicated unit tests

Exercised indirectly via integration/eval suites, but no unit-level tripwire:

- `inference/dev_agent.py` — the planner / decision loop
- `inference/model_router.py` — routing & model selection
- `storage/db.py` — schema source of truth (AGENTS.md #1)
- `core/command_executor.py`, `core/hybrid_coordinator.py`, `core/fusion_engine.py` — 60 Hz pipeline core
- All 6 `mcp_server/tools/*.py` (mouse, keyboard, screen, windows, handwriting) — desktop-action surface

Also: ~17 DB tests conditionally **skip on missing `aiosqlite`** — would benefit from a fixture stub
so the gate runs in CI regardless of the runtime dep.

- **Action:** Prioritize `model_router.py` and `db.py` unit tests first (highest blast radius).

## 🟡 Gap 5 — Two genuinely unfinished features

- **UDIFF edit format** — `inference/edit_format.py:38` reserved (spec R4), not implemented;
  degrades gracefully to hashline. Tested building block, no live path.
- **Scheduler not fully wired in FusionEngine** — `core/fusion_engine.py:946` falls back to bare
  fire-and-forget when the scheduler isn't wired, losing the bounded-pool latency/ordering guarantees.

## 🟡 Gap 6 — Eval baselines worth a second look

- `rag_ablation` baseline = **0.2** (80% miss) — confirm whether this is an intentional ablation floor
  or a real RAG regression target.
- `dev_trajectory` is a **safety gate at 0.7273** — mid-accuracy for plan correctness.
- `intent_satisfaction` (20 cases) has **no locked baseline** — suite exists but isn't gating yet.

---

## ✅ Verified clean (non-gaps)

- No `NotImplementedError` in production code.
- No bare `except: pass` — all exception paths log + return a sensible default.
- No stub functions (`pass`-only bodies) or hardcoded mocks where real logic is expected.
- Graceful-degrade discipline holds (ChromaDB→Jaccard, Whisper/MCP/sandbox absent → logged fallback).

## Feature-flag inventory (ship-dark discipline — informational, not gaps)

**OFF by default (experimental):** `workflow_orchestration.enabled`, `conversation_mode.enabled`,
`self_skilling.enabled`, `DA_DOMAIN_LEARN`, `DA_TRAJECTORY_REDUCE`, `DA_A2UI_CLICK_TARGETS`.

**ON by default (opt-out):** `DA_PLAN_REPAIR`, `DA_CRITIC`, `DA_TESTER`, `DA_NEURAL_VAD`, `DA_TRACE`,
`DA_SANDBOX`, `DA_MATH_CAS_VERIFY`, `DA_CURSOR_GRAVITY`, `DA_COMMAND_WARMUP`, `DA_BINARY_AUDIO`,
`wsl_terminal_routing.enabled`.

---

## Work completed this session (2026-06-25)

- **PR #136 merge conflicts resolved** (master → `feat/browser-ui-testing`): 5 union-merge conflicts
  across `desktop_mcp_server.py` (docstring, imports-inside-shim, 2× tool registration, dispatcher)
  and `CLAUDE.md` (gotcha bullet). Resolved in an isolated worktree (conversation-mode work untouched).
  Verified: no markers, compiles, imports clean, `list_tools()` = 26 tools (0 missing / 0 dupes),
  93 tests pass / 1 Playwright-skip. Pushed `64aa0eb..f321ed8`; PR now MERGEABLE.

## Next-session starting point

1. Commit conversation-mode → `feat/conversation-mode` (Gap 1).
2. Merge #134 (EDIT_FILE), then #136.
3. Decide WorkflowRunner trigger vs. explicit defer (Gap 2).
4. Add `model_router.py` + `db.py` unit tests (Gap 4).
