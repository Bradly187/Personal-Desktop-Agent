# Daily Review — 2026-06-06

*(Covers the agent-orchestration hardening sprint committed late 2026-06-05
→ 06-06, which the `2026-06-05-daily-review.md` was written too early to capture,
plus the Opus 4.8 cloud switch and today's magnetic-cursor tuning.)*

## Summary

An AIOS-hardening sprint on top of PR **#32** (`fix/tilt-tap-click`, already
re-opened after the #31 merge). Six commits turn the scheduler / dev-agent /
governor stack from "wired" into "fail-safe": closed-loop ReAct planning,
crash-recoverable plan ledger, deny-by-default destructive approvals, live
governor↔router flare eviction, release-safe scheduler metrics, and opt-in
cross-layer command tracing. Closed with the cloud dev path moved to
**Opus 4.8**. Branch is pushed and in sync with origin (0 ahead / 0 behind).

---

## Landed on `fix/tilt-tap-click` (PR #32, pushed)

6 commits since the #31 merge; **+3279 / −1112** across 42 files.

### `fix(inference)` — router-derived flare eviction `b989d88`
- `ResourceGovernor` now evicts the **router-derived heavy specialist set** on a
  flare (was a hardcoded, now-stale `qwen3-vl:30b`) and sleeps the vLLM pool.
  New `set_model_router()`; falls back to a known-good list when no router wired.
- `ModelRouter`: `heavy_model_names()` + `sleep_specialists()` so the governor
  tracks the live lineup.

### `feat(dev-agent)` — fail-safe approvals + closed-loop ReAct + durable ledger `46a5f17`
- **Approval (Fix #2):** destructive plans/ops fail-safe to **DENY** on
  silence/ambiguity/hardware failure (only an explicit "yes" or a prior
  whole-plan grant proceeds); read-only plans keep auto-approve. Mirrors the
  hardened voice gate.
- **Closed-loop (B2):** `plan_and_run` is now observe→act→replan-on-failure with
  bounded `_replan` (`MAX_REPLANS=2`), one retry for read-only verbs, hard halt
  instead of compounding failures. Fixed `_parse_plan`: its non-greedy args regex
  was silently dropping **every** step argument (RUN_TERMINAL ran with no command).
- **Durable ledger (B3):** `agent_runs` gains a status lifecycle (+migration) +
  start/update/mark_interrupted/get_interrupted; plans write running→step→finalize;
  startup reconciles orphaned runs to `interrupted`; `resume_pending_plan()` offers
  a voice-gated resume.

### `feat(scheduler)` — resource invariants + release-safe metrics + teardown `d23d9ed`
- Docstrings now state the real design: ACCESSIBILITY/VOICE/GESTURE run
  uncapped/concurrent (priority only bites under backlog); the single-permit
  `_dev_sem` is the actual flare protection. Added a **Resource Invariants** block
  (single-permit re-entrancy self-deadlock, `submit_plan` 300 s hold hazard) and a
  deadlock-safe fan-out note.
- `_dev_inflight` incremented after acquiring the permit, decremented in `finally`
  → gauge can't leak on timeout/exception/cancel.
- `stop()` cancels in-flight dispatched tasks so no task/permit lingers past
  shutdown.
- New `scheduler_queue_depth` / `scheduler_dev_inflight` gauges.

### `chore(main)` — governor↔router wiring + crash recovery + metrics `68e66e1`
- `governor.set_model_router(router)` (flare eviction targets the live lineup),
  `mark_interrupted_runs()` at startup (reconciles plans orphaned by a crash, logs
  a resume hint), `scheduler.set_metrics(m)` (queue-depth / dev-inflight in
  `/metrics`).

### `feat(observability)` — opt-in cross-layer per-command tracing `87eccef`
- New `monitoring/trace.py`: `TraceRecorder` (ring buffer, ContextVar,
  `record_span`/`timed`/`new_trace`), `get_tracer()` singleton. **Zero-cost no-op
  unless `DA_TRACE` is set.**
- `trace_id` rides on the `Command` dataclass (survives the scheduler
  `create_task` hop); a ContextVar carries it through coordinator→router→executor.
- Reconstructs one command's journey: enqueue → dispatch → route_decision →
  inference → execute. `commands.trace_id` column (+migration); `GET /trace` and
  `/trace/{id}` on the metrics app.

### `chore(cloud)` — Opus 4.8 dev path; normalize Haiku alias `7c4cf9a`
- `CloudDevAgent` defaults to **`claude-opus-4-8`** (was `claude-sonnet-4-6`) for
  free-form dev generation / long-horizon agentic reasoning. No request-shape
  change (already passes no sampling params + adaptive/disabled thinking +
  streaming, which Opus 4.8 requires).
- Command-path cloud fallback ID normalized `claude-haiku-4-5-20251001` →
  `claude-haiku-4-5` alias (cosmetic).
- Out of scope: `vision_grounder.py` Sonnet 4.6 vision fallback (local qwen3-vl is
  primary there).

---

## Working tree (uncommitted)

Today's magnetic-cursor tuning, not yet committed:
- `core/fusion_engine.py`: `_gravity_max_pull` **18 → 22** px (stronger edge nudge).
- `core/command_executor.py`: `DA_SNAP_RADIUS_PX` default **200 → 300** px (wider
  tilt-tap snap capture).

Untracked artifacts: `benchmark_results.json` (regenerated 07:04) and
`docs/website_diagrams.html` (new 13:36). Neither is gitignored — decide whether
to commit `website_diagrams.html` or ignore the regenerated benchmark JSON.

---

## Verification
- Test suite: **673** `test_*` functions across **60** `tests/test_*.py` files
  (full-run pass count not re-measured this session). New this sprint:
  `test_scheduler.py`, `test_trace.py`, expanded `test_resource_governor.py`.

---

## Housekeeping (this review)
- `CLAUDE.md` status header re-dated to 2026-06-06 with a new
  "Agent-orchestration hardening" Done block; test-suite line refreshed.
- `MEMORY.md` index corrected: the sprint **is pushed** and in sync with
  `origin/fix/tilt-tap-click` (the prior "local-only — not pushed" + stale commit
  hashes `6ead6b2`/`c6645c5` were left over from a pre-amend state — current tip is
  `7c4cf9a`).

## Open Items (carried / new)
- **SVT fast-path for `ResourceGovernor`** (carried) — 5 s poll → up to 5 s before
  VRAM release on an SVT attack; a `set_manual_pain_day(True)` callback would cut
  this to < 1 s.
- **iPad end-to-end magnetic verification** (carried) — gravity stability smoke is
  green; real-hardware confirmation (tilt cursor sticks to buttons), now with the
  18→22 / 200→300 tuning, still pending.
- **Branch sprawl** (new) — ~7 local branches with `: gone` upstreams (merged or
  deleted PRs: `claude/*`, `fix/local-inference-circuit-breaker`,
  `fix/approval-gate-confirmation-token`, `feat/cluster-tier2-services`). Safe to
  prune. Note `feat/ollama-030-orchestration` + `feat/durable-slo-sandbox` still
  have live upstreams.
- **Memory superstate stale** (carried) — `MEMORY.md` still points at the
  2026-05-11 superstate; a fresh one covering gaze removal → AIOS alignment →
  cluster tier → goal sessions → magnetic rework → this orchestration sprint is
  overdue.
- **`aios_sdk` package** (carried, low priority for a single-user system).
