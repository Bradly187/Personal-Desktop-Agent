# Sprint Plan — "Honest, Actionable Failure" — 2026-06-19

Tracked plan for the three highest-leverage items from
[`2026-06-19-agentic-practices-assessment.md`](2026-06-19-agentic-practices-assessment.md).
Findings reference `2026-06-16-error-handling-gap-analysis.md` (E#) and the roadmap (workstream D).

**Theme:** stop the agent from reporting success it didn't achieve, stop its durable records from
lying, and make the eval flywheel spin on real data instead of projections.

**Branch convention:** one PR per sprint off `master`. Each is independently shippable.
Sequence EH-1 → EH-2 (disjoint-ish files; EH-2 touches the destructive path, land alone). Data
accrual (D) runs in the background the entire time.

```
S1  Close the verification loop      ← land first; accessibility-critical
S2  Durable-failure integrity        ← own PR; destructive path; after S1
S3  Eval data accrual                ← background; gate-readiness, not code
```

Progress legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Sprint S1 — Close the verification loop  *(EH-1)*

> **STATUS (2026-06-19): ALREADY SHIPPED — no code work.** Validating the findings against the
> code showed EH-1 was already implemented + tested in master by **PR #88** (merged 2026-06-16),
> *before* this plan was written. Confirmed: executor `status="verify_failed"`
> (`command_executor.py:691`) and `status="resolve_miss"` via `TargetResolutionError` (`:601`);
> `_resolve_coords(strict_target=…)` raises instead of cursor-clicking a named miss (`:979`);
> coordinator one-shot vision→UIA re-resolve + spoken CLARIFY (`hybrid_coordinator.py:2196-2228`);
> `route()` computes `success` from the final status (`:1545`) so verified-failures reach
> `record_failure` (`:1631`), never `record_success`. Fold-ins E10/E16/E17 done; `tests/test_verify_loop.py`
> green (23). **Residuals deferred as marginal:** E20 iPad toast on no-coordinator drop (WARNING already
> logged, `fusion_engine.py:955`; the drop is a startup-misconfig path); S1.3 explicit `resolved_by`
> tier label (the next-tier retry already works for the dominant case). Sprint closed; effort moved to S2.

**Goal:** a failed desktop action self-corrects once or honestly asks, instead of silently clicking
the cursor and recording success. Highest leverage; directly serves the accessibility mission.

**Findings:** E1, E2 (fold in E10, E16, E17, E20 — cheap hot-path honesty).
**Risk/effort:** Medium (touches the accessibility hot path). ~1–2 days. Keep retry strictly
one-shot behind the existing verify gate so latency stays bounded.

### Tasks
- [x] **S1.1** `desktop/action_verifier.py` — expose the success/fail verdict cleanly to the
      executor (no change to the pixel-diff itself).
- [x] **S1.2** `core/command_executor.py` `execute()` (E1) — when verb ∈ `VERIFIABLE_VERBS` and
      `vr.success is False`, return `status="verify_failed"` carrying `verification` + the resolver
      tier used. Non-verifiable verbs unchanged.
- [x] **S1.3** `core/command_executor.py` `_resolve_coords` (E2) — thread `target_specified: bool`
      and a `resolved_by` label (`explicit|uia|vision|gaze|cursor`) to the caller. When a target was
      named and only the cursor fallback remains, return a `RESOLVE_MISS` sentinel (do **not** click
      the cursor). Keep cursor fallback only when no target was named (tilt/voice-click).
- [x] **S1.4** `core/hybrid_coordinator.py` `_execute_action` — on `RESOLVE_MISS` → CLARIFY
      ("I couldn't find <target>…"); on `verify_failed` → **one** re-resolve forcing the next tier
      past `resolved_by`, re-verify; still failed → CLARIFY + record **failure**. Cap at one retry.
- [x] **S1.5** `core/hybrid_coordinator.py` `route()` outcome (E1) — compute `success` from the
      *final* verdict, not `status=="ok"`. Verified-failed must reach `record_failure`, never
      `record_success`.
- [x] **S1.6** Hot-path honesty fold-ins: Gate-1 gesture-fail logs a `discarded` command row (E10);
      sanitize raw exception strings in CLARIFY (E16); populate `error_msg` on CLARIFY-as-failure
      rows (E17); `fusion_engine._emit` no-coordinator drop → WARNING + toast (E20).
- [x] **S1.7** Tests — `tests/test_verify_loop.py`: verify-fail → one re-resolve → CLARIFY;
      verify-pass → no retry; non-verifiable verb unchanged; `RESOLVE_MISS` → CLARIFY not
      cursor-click; outcome recorded as failure; retry capped at one (assert no storm). Extend
      `test_command_executor.py` (tier labelling + sentinel), `test_action_verifier.py` (status map).
- [x] **S1.8** Re-run model-free eval gates (`router_domains`, `skill_triggers`) + full pytest;
      confirm no regression. Add a verify-loop trajectory case if applicable.

**Exit:** a named-target CLICK with no UIA/vision hit emits CLARIFY (never a cursor click); a
verified-failed action is recorded as failure and triggers exactly one re-resolve.

---

## Sprint S2 — Durable-failure integrity  *(EH-2)*

**Goal:** records that exist to survive failure must reflect reality and must not be lost. The
destructive dev-agent path — apply fail-safe-to-DENY discipline, full saga coverage, no shortcuts.

**Findings:** E3, E4, E5, E6 (fold in E18, E19 planner-honesty).
**Risk/effort:** Medium-High. Own PR, not bundled. ~2 days.

### Tasks
- [ ] **S2.1** Compensation status (E3) — `inference/dev_agent.py` / `storage/db.py`: set saga
      compensation row `done` **only on success**, `failed` on exception (keep cascade-continue). On
      any `failed` compensation, fire `_record_escalation(reason="compensation_failed")`.
- [ ] **S2.2** Escalation durability (E4) — set `_escalated_this_run` **only after** the row commits.
      On DB-unavailable, append to a JSON sidecar (`~/.claude/escalations_pending.jsonl`); add
      `reconcile_escalations()` called alongside `mark_interrupted_runs` at next healthy boot. Don't
      let TTS claim "added to review queue" unless a row persisted.
- [ ] **S2.3** Register-before-write saga (E6) — move compensation registration to **snapshot time**
      (before the write), mark `active`; finalize `pending`→`active`→`done`. A partial-write failure
      (`success=False`) still rolls back to the pre-write snapshot.
- [ ] **S2.4** `RESTORE_FILE` un-snapshottable (E5) — when no pre-write snapshot exists (file ≥256 KB)
      and the file existed, mark compensation `skipped` (not `done`) and escalate, rather than
      reporting success with post-write content in place. Size-bounded temp snapshot where feasible.
- [ ] **S2.5** Planner honesty — a replan that parses to zero real steps counts toward `MAX_REPLANS`
      instead of fabricating an `EXPLAIN` (E18); a DAG with "pending but no ready" logs WARNING with
      the unmet-dep set before sequential fallback (E19).
- [ ] **S2.6** Tests — `tests/test_saga_integrity.py` (compensation-throws → row `failed` +
      escalation; partial-write → snapshot restored; un-snapshottable → `skipped`+escalation);
      `tests/test_escalation_durability.py` (DB-down → sidecar written → reconciled on boot;
      `_escalated_this_run` not set on failed commit). Extend `test_dev_agent.py` (replan zero-step,
      DAG-cycle warning). Migration round-trip test if a compensation `status` enum value is added.

**Exit:** no saga row ever reports a rollback that didn't happen; no escalation is announced that
isn't persisted; a DB outage at escalation time is recovered on next boot.

---

## Sprint S3 — Eval data accrual  *(roadmap workstream D)*

**Goal:** cross the data thresholds so the regression gates and routing classifier rest on real
logged usage, not projections. This is "use it daily with instrumentation on," plus light tooling to
harvest and lock — **not** a code-correctness sprint.

**Targets:** routing classifier corpus 200+ labelled cases (≈11 today); per-tier CLICK-success
(vision/UIA/verifier) logged as real within-subject numbers (replace the projected 42→78→88→92 %);
gesture-confidence rows seeded (unblocked once real depth flows — see roadmap A1–A3).
**Risk/effort:** Low (mostly accrual + harvest). Spans the whole period; check weekly.

### Tasks
- [ ] **S3.1** Confirm instrumentation is on by default in daily runs — `routing_log`, per-tier
      CLICK outcome, latency, gesture-confidence rows all writing to `agent.db`. (S1 adds the
      verify-failed/failure rows, which makes this corpus *honest* — sequence S3 harvesting after S1
      lands so gold cases aren't poisoned by false-success rows.)
- [ ] **S3.2** Weekly: `harvest_from_agent_db` → append gold cases to `evals/suites/*.jsonl` from
      `commands.corrected_to`; review for label quality before committing.
- [ ] **S3.3** When `router_domains` corpus ≥ 200 cases, re-lock the baseline
      (`python -m evals.run --suite router_domains --predictor router --update`) and record the new
      floor; note the sample size in the baseline file.
- [ ] **S3.4** Replace projected CLICK-success figures in `JUNE_2026_ROADMAP.md` / assessment with
      logged within-subject results once N is adequate; flag any tier below target as its own finding.
- [ ] **S3.5** Track the count weekly in a short note under `docs/daily/` (current N vs. 200 target
      per suite) so the threshold crossing is visible.

**Exit:** routing classifier and CLICK-success gates are backed by ≥200 real cases each; baselines
re-locked on that data; projections removed from the docs.

---

## Tracking

| Sprint | Findings | Status | PR | Owner |
|--------|----------|--------|----|----|
| S1 — verification loop | E1, E2 (+E10/16/17/20) | `[x]` already shipped (PR #88, 2026-06-16) | #88 | — |
| S2 — durable-failure integrity | E3, E4, E5, E6 (+E18/19) | `[ ]` not started | — | — |
| S3 — eval data accrual | roadmap D | `[ ]` background | — | — |

**Dependencies:** S1 before S2 (land the hot-path honesty first); S3 harvesting *after* S1 (so the
gold corpus isn't seeded with false-success rows). EH-4 resilience wiring (E7/E8/E9/E15) is out of
scope here — independent, mechanical, can land any time.

**Baseline before starting:** full pytest (~181 files / ~1,400+ tests) + `scripts/run_evals.ps1`
gates green; `git log` / open-PR scan per `AGENTS.md` rule #8 to avoid colliding with in-flight work
(note: PR #87 robustness work and any EH-* branches).
