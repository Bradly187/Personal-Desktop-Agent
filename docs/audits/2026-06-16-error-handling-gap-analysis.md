# Error Handling, Failed-Task Handling & Self-Correction — Gap Analysis + Sprint Plan — 2026-06-16

Source: five parallel subsystem sweeps over `inference/dev_agent.py`, `core/` (coordinator,
fusion, scheduler, supervisor, governor, circuit_breaker, crash_marker, rate_limiter,
proactive_scheduler, event_rule_engine, email_watcher, notifier), `desktop/`
(action_verifier, vision_grounder, ui_automation, command_executor), and `storage/db.py`
(goal_queue / agent_runs / dev_escalations / saga tables).

Branch context: `master` @ `ee3febd`, tree clean. **Open PR #87** (`fix(robustness): kill
stuck processes & subagents`) is NOT merged into this worktree — the sweeps read pre-#87
code. Findings already closed by #87 are tagged **[#87]** and excluded from the sprints below.

## Thesis

The system **detects death** well (process exit, `task.done()`, crash marker, interrupted-run
reconciliation, goal-queue lease) and **replans** adequately, but is weak on three axes:

1. **Self-correction is observational.** Action verification computes a failed/succeeded
   verdict and then *does nothing with it* — no retry, no re-resolve, no CLARIFY. The loop is
   open.
2. **Durable-failure writes are best-effort.** The records meant to survive failure
   (escalations, saga compensations) can be silently lost or falsely marked `done`.
3. **Failure signals dead-end.** Escalations and proactive notifications accumulate with
   nothing surfacing or retrying them; an offline iPad drops the notification entirely.

PR #87 attacks a fourth axis — **stuckness vs. death** (alive-but-wedged loops/processes) —
which was a real hole. It does not touch axes 1–3.

---

## 1. Findings (record)

Severity after review. Class ∈ {correctness, durability, resilience, observability, latency}.
`E#` numbering is local to this doc.

| # | Sev | Class | Location | Issue | Fix direction |
|---|-----|-------|----------|-------|---------------|
| E1 | HIGH | correctness | `desktop/action_verifier.py` + `core/command_executor.py:525-555` + `core/hybrid_coordinator.py:~1449,1515` | Verification computes `success=False` (no visible change) but executor returns `status="ok"` regardless and the coordinator computes `success = status=="ok"`, **never inspecting `result["verification"]`**. A verified-failed click is recorded as success and fed to `trainer.record_success`. No retry / re-resolve / CLARIFY. | Executor: a `verifiable` verb whose `verify.success=False` returns `status="verify_failed"`. Coordinator: on `verify_failed`, re-resolve via the *next* fallback tier once, re-verify, else emit CLARIFY; record as failure. |
| E2 | HIGH | correctness | `core/command_executor.py:792-826` `_resolve_coords` | Chain explicit→UIA→`gaze_coords`→**cursor position** degrades a named target to a cursor click silently; terminal fallback always "succeeds". `CLICK "Submit"` with no UIA/vision hit clicks wherever the cursor is, no CLARIFY. With E1 this is then logged as success. | When `target` was specified and every resolver missed, return a sentinel that makes `_execute_action` emit CLARIFY instead of clicking the cursor. Keep cursor-fallback only when no target was named (tilt/voice-click). |
| E3 | HIGH | durability | `inference/dev_agent.py:~1232-1249` / `storage/db.py:~3190` | Saga compensation marks the row `done` **unconditionally** even when the rollback step (e.g. `RESTORE_FILE`) throws — logged ERROR but status says `done`. Zombie compensation: audit trail shows a rollback that never happened. | Set `done` only on success; `failed` on exception (keep the cascade-continue behavior). Surface a compensation-failed escalation. |
| E4 | HIGH | durability | `inference/dev_agent.py:1193-1207` `_record_escalation` | If `agent.db` is unavailable / `insert_escalation` throws when a plan exhausts `MAX_REPLANS`/`MAX_STEPS`, the exception is swallowed (WARNING) — **no escalation row persists** — yet `_escalated_this_run` is set so the TTS still claims "added to review queue". User told it queued; nothing there. | Don't set `_escalated_this_run` unless the row committed. On DB-unavailable, write a durable fallback (JSON sidecar under `~/.claude/`) and reconcile into `dev_escalations` on next healthy boot. |
| E5 | MED | durability | `inference/dev_agent.py:1266-1270` `RESTORE_FILE` | When no pre-write snapshot exists (file ≥256 KB) and the file existed, compensation **leaves the post-write content in place** while reporting success. Silent data-correctness gap in rollback. | Either snapshot large files to a temp path (size-bounded) or, when un-snapshottable, mark compensation `skipped` (not `done`) and escalate so the user knows the write stands. |
| E6 | MED | durability | `inference/dev_agent.py:1128` | Compensation registered only `if step.success`; a `WRITE_FILE` that writes partially then throws (`success=False`) registers **no** compensation → half-written file persists through rollback though the snapshot was taken. | Register the compensation at snapshot time (before the write), mark it active on entry; on partial failure the rollback restores the pre-write snapshot. |
| E7 | MED | resilience | `inference/local_inference.py:~1007 (VLLMServerInference), ~1349 (LlamaCppInference)` | No `CircuitBreaker` on the HTTP-backed alternate backends; a hung vLLM/llama-server pays the full 30 s timeout on **every** request with no fast-fail. (`OllamaInference` has one; #87 adds slow-call tripping to it only.) | Wire the same `CircuitBreaker` + `finally`-based outcome reporting into both backends. |
| E8 | MED | resilience | `core/resource_governor.py:374-395` `_evict_heavy_models` | Each `keep_alive=0` eviction POST blocks 5 s when Ollama is down, repeated per model per poll — accumulating latency precisely during a pain flare. No breaker / backoff. | One-shot breaker or exponential backoff on the eviction endpoint; skip remaining evictions for the cooldown once one fails. |
| E9 | MED | latency | `core/hybrid_coordinator.py:1699-1726` Gate 3 | `await asyncio.to_thread(vram.free_vram_gb)` has no `wait_for`; a hung NVML/driver blocks the command path. 2 s cache masks repeats, not a cold stall. Fail-open is correct; the missing timeout is the gap. (Overlaps the prior-audit #6 NVML-on-loop direction — confirm whether the background poller landed.) | Bound the probe with `asyncio.wait_for(..., 1.0)`; on timeout fail-open from cache. Prefer a background VRAM poller (single source). |
| E10 | MED | observability | `core/hybrid_coordinator.py:1416` Gate 1 (gesture-fail) | A low-confidence gesture command is **discarded with no DB row and no CLARIFY** → invisible to retraining and to the user; lost signal. | Log a `discarded` command row (gate label `gate1_gesture_conf`); optionally a soft toast. |
| E11 | MED | observability | `core/fusion_engine.py:949` 60 Hz tick catch-all | `except Exception: log.error(...)` then continues; the unconsumed sensor command (`_tilt/_voice/_gesture` already popped) is dropped silently. | Re-queue or explicitly account the dropped command; raise log level and include the lost source/text. |
| E12 | MED | durability | `core/notifier.py:24-43` | Both TTS and `bridge.broadcast_json` are best-effort, exceptions at DEBUG, `notify()` returns `None`. Offline iPad ⇒ proactive notification **dropped, no queue, no delivery signal**. Confirms the long-standing store-and-forward gap. | Persist undelivered notifications (small table / JSON sidecar); flush on iPad reconnect; return a delivery result so callers can decide. |
| E13 | MED | observability | `inference/dev_agent.py` (escalation backlog) + `storage/db.py:dev_escalations` | Backlog only readable via voice ("review queue"). No startup count, no proactive nudge — failed dev goals accumulate invisibly. | On startup / via `ProactiveScheduler`, if `count_pending_escalations() > 0`, fire a `Notifier` nudge ("N plans need review"). |
| E14 | MED | durability | `core/email_watcher.py` | On a Google-skill RECONNECT / skill-unregistered window the batch is discarded; mail that ages out of "unread" before auth is restored never fires `email.arrived`. Dedup state is durable, but there's no "arrived while blind" backlog. | On auth/skill failure, keep `_baselined` but do **not** advance past unseen ids; surface a one-time "Gmail watcher blind" notice and re-scan a wider window on recovery. |
| E15 | MED | durability | `storage/db.py requeue_stale_running` (lease) | Lease has **no TTL**: a goal stuck `running` under a *live-but-wedged* PID is never requeued (only dead-PID rows recover). #87's heartbeat restarts the loop but the claimed row stays `running`. | Add `claimed_at`-based lease expiry: requeue `running` rows older than a TTL even if `owner_pid` is alive (it'll be a fresh process post-restart). |
| E16 | LOW | observability | `inference/local_inference.py:514`; `core/hybrid_coordinator.py:259` | Raw `aiohttp`/SDK exception strings leak into user-facing CLARIFY ("Connection refused", SSL errors). | Map to a stable sanitized sentence; keep the raw text in logs only. |
| E17 | LOW | observability | `core/hybrid_coordinator.py:1299,1555` outcome logging | `error_msg` populated only on top-level exception; CLARIFY-as-failure rows store `error_msg=NULL` with the error buried in `action`. Harder root-cause. | Populate `error_msg` whenever the action is a `CLARIFY <reason>` failure. |
| E18 | LOW | correctness | `inference/dev_agent.py:579-583` | An unparseable replan response silently becomes a single `EXPLAIN` step instead of failing the goal. | If both JSON and regex parse to zero real steps on a *replan*, treat as replan-failure (count toward `MAX_REPLANS`), don't fabricate an EXPLAIN. |
| E19 | LOW | correctness | `inference/dev_agent.py:903-908` `_run_dag_waves` | A planner-declared dependency cycle silently falls back to sequential (deps ignored) — possible ordering violation, no warning. | Detect a cycle (no ready steps but pending remain), log WARNING, and prefer plan-order sequential only after recording the unmet-dep set. |
| E20 | LOW | observability | `core/fusion_engine.py:919` `_emit` | No coordinator set ⇒ command dropped at WARNING, no CLARIFY (startup-window race). | Buffer briefly or emit a "still starting up" toast; raise to WARNING with the dropped command. |
| E21 | LOW | resilience | `core/supervisor.py:187` `enabled()` gate | If a subsystem's `_running` flag is cleared (accidentally), the supervisor silently stops watching it — a crash can be masked. | Log once when a previously-supervised subsystem becomes `enabled()==False` while the supervisor runs. |

### Closed by PR #87 (do not re-fix — verify on merge) — tagged **[#87]**
- Per-step timeout in dev-agent (`asyncio.wait_for(180s)`, 30 s skill stdio) — a wedged step
  no longer holds the single dev permit to the 300 s plan ceiling.
- Heartbeat watchdog for periodic supervised loops (governor / proactive / email) — restarts
  *alive-but-wedged* loops; `cancel_if_alive` cancels the stuck task first.
- Process-tree kill (`core/proc_utils.py`) for sandbox / npm / code-eval — no orphaned
  grandchildren.
- Slow-call tripping added to the Ollama `CircuitBreaker`.
- OPEN-verb detached launch + handle reaping.
- **Caveat to confirm on merge:** event-driven loops (scheduler worker, event-rule engine)
  deliberately get **no** heartbeat. A wedged scheduler *worker* is still only caught by
  `is_healthy()` (task-done), not by stall. Confirm that's acceptable or add a
  dispatch-progress heartbeat. (Related: **E15** lease TTL is still open after #87.)

### Verified healthy (no defect)
Malformed-LLM verb → CLARIFY (`_VALID_COMMAND_VERBS` guard) throughout; destructive ops
fail-safe to DENY on silence/ambiguity/timeout; goal-queue idempotency revive across terminal
states + dead-PID requeue; `agent_runs` `mark_interrupted_runs` reconciliation at startup;
`record_failure` counters never touch positive few-shot / PreferenceModel / SemanticMemory;
read-only retry restricted to idempotent verbs; saga cascade continues on a single
compensation failure (only the *status write*, E3, is wrong); EventRuleEngine same-tick burst
cooldown claimed before await; EmailWatcher dedup state atomic + restart-durable.

---

## 2. Sprint EH-1 — Close the verification loop  *(recommended first — highest leverage)*

**Goal:** make a failed desktop action self-correct or honestly ask, instead of silently
clicking the cursor and recording success. Directly serves the accessibility mission — a
silently-missed click is worse than an honest "I couldn't find Submit."

**Findings:** E1, E2 (+ E10, E16, E17, E20 fold in cheaply — hot-path honesty).

**Files & changes:**
- `desktop/action_verifier.py`: no behavior change to the diff; expose the verdict cleanly.
- `core/command_executor.py`
  - `execute()` (E1): when the verb is in `VERIFIABLE_VERBS` and `vr.success is False`, return
    `status="verify_failed"` (carry `verification` + the resolver tier used). Non-verifiable
    verbs unchanged.
  - `_resolve_coords` (E2): thread a `target_specified: bool` and a `resolved_by` label
    (`explicit|uia|vision|gaze|cursor`) out to the caller. When `target_specified` and the only
    resolver left is the cursor fallback, return a `RESOLVE_MISS` sentinel.
- `core/hybrid_coordinator.py`
  - `_execute_action` (E1/E2): on `RESOLVE_MISS` → CLARIFY ("I couldn't find <target>…").
    On `verify_failed` → **one** re-resolve forcing the next tier past `resolved_by`, re-verify;
    still failed ⇒ CLARIFY + record failure (not success). Cap at one retry (no loops).
  - `route()` outcome (E1): compute `success` from the *final* verdict, not `status=="ok"`.
  - Gate 1 gesture-fail (E10): insert a `discarded` command row with gate label.
  - CLARIFY/exception sanitization (E16) + `error_msg` population for CLARIFY failures (E17).
- `core/fusion_engine.py` `_emit` (E20): raise the no-coordinator drop to WARNING + toast.

**Test strategy:** `tests/test_verify_loop.py` — verify-fail → one re-resolve → CLARIFY;
verify-pass → no retry; non-verifiable verb → unchanged; `RESOLVE_MISS` → CLARIFY not
cursor-click; outcome recorded as failure on verify-fail. Extend `test_command_executor.py`
(resolver-tier labelling, sentinel), `test_action_verifier.py` (status mapping). Assert the
re-resolve is capped at one (no retry storm). Full suite baseline ~ (139 test files; run
`scripts/run_evals.ps1` router/gate suites unchanged).

**Risk/effort:** Medium. Touches the accessibility hot path — keep the retry strictly
one-shot and behind the existing verify gate so latency is bounded. ~1–2 days.

---

## 3. Sprint EH-2 — Durable-failure integrity

**Goal:** the records that exist to survive failure must reflect reality and must not be lost.

**Findings:** E3, E4, E5, E6 (+ E18, E19 planner-honesty fold in).

**Files & changes:**
- `inference/dev_agent.py` / `storage/db.py`
  - Compensation status (E3): `done` only on success, `failed` on exception; on any `failed`
    compensation, `_record_escalation(reason="compensation_failed")`.
  - `_record_escalation` durability (E4): only set `_escalated_this_run` after the row commits;
    on DB-unavailable, append to a JSON sidecar (`~/.claude/escalations_pending.jsonl`) and
    reconcile into `dev_escalations` at next healthy boot (new `reconcile_escalations()` called
    alongside `mark_interrupted_runs`).
  - Register-before-write saga (E6): move compensation registration to snapshot time, mark it
    `active`; finalize `pending`→`active`→`done` so a partial-write failure still rolls back.
  - `RESTORE_FILE` un-snapshottable (E5): mark `skipped` + escalate rather than reporting
    `done`; size-bounded temp snapshot for large files where feasible.
  - Replan parse (E18): a replan that parses to zero real steps counts toward `MAX_REPLANS`
    rather than fabricating an `EXPLAIN`.
  - DAG cycle (E19): detect "pending but no ready", log WARNING with the unmet-dep set before
    sequential fallback.

**Test strategy:** `tests/test_saga_integrity.py` — compensation-throws → row `failed` +
escalation; partial-write → snapshot restored; un-snapshottable restore → `skipped`+escalation.
`tests/test_escalation_durability.py` — DB-down at escalation → sidecar written → reconciled on
boot; `_escalated_this_run` not set when commit fails. Extend `test_dev_agent.py` (replan
zero-step, DAG cycle warning). Migration round-trip if a compensation `status` enum value is
added.

**Risk/effort:** Medium-High. This is the destructive dev-agent path — apply the
fail-safe-to-DENY discipline, full saga coverage, no shortcuts. Its own sprint, not bundled.
~2 days.

---

## 4. Sprint EH-3 — Surface the backlogs (store-and-forward)

**Goal:** stop failure signals from dead-ending. Deliver notifications that currently vanish;
nudge the user about backlogs they never see.

**Findings:** E12, E13, E14.

**Files & changes:**
- `core/notifier.py` (E12): persist undelivered pushes (small `pending_notifications` table or
  JSON sidecar); `notify()` returns a delivery result; flush queue on iPad reconnect
  (`ipad_bridge` connect hook). TTS stays best-effort.
- `core/proactive_scheduler.py` / startup (E13): if `count_pending_escalations() > 0`, fire a
  `Notifier` nudge once per session ("N plans need review — say 'review queue'").
- `core/email_watcher.py` (E14): on RECONNECT/skill-missing, hold the unseen-id frontier (don't
  silently absorb), surface a one-time "Gmail watcher blind" notice, widen the re-scan window
  on recovery.

**Test strategy:** `tests/test_notifier_store_forward.py` (offline → queued → flushed on
reconnect; delivery result returned), extend `test_proactive_scheduler.py` (escalation nudge
fires once), `test_email_watcher.py` (auth-blind window holds frontier + notice).

**Risk/effort:** Low-Medium. All primitives already exist (`Notifier`, `ProactiveScheduler`,
bridge connect events). ~1 day.

---

## 5. Sprint EH-4 — Resilience wiring (mechanical)

**Goal:** the cheap, low-risk breaker/timeout/lease gaps.

**Findings:** E7, E8, E9, E15, E21.

**Files & changes:**
- `inference/local_inference.py` (E7): wire `CircuitBreaker` + `finally` outcome reporting into
  `VLLMServerInference` and `LlamaCppInference`, mirroring `OllamaInference`.
- `core/resource_governor.py` (E8): wrap eviction in a one-shot breaker / backoff; skip the rest
  of the eviction batch for the cooldown once one POST fails.
- `core/hybrid_coordinator.py` Gate 3 (E9): `asyncio.wait_for(..., 1.0)` around the VRAM probe,
  fail-open from cache on timeout (or confirm the background poller from the prior audit shipped
  and just read its cache).
- `storage/db.py` `requeue_stale_running` (E15): `claimed_at` lease TTL — requeue `running` rows
  older than the TTL even under a live `owner_pid` (post-restart it's a new process anyway).
- `core/supervisor.py` (E21): one-time log when a supervised subsystem flips to
  `enabled()==False`.

**Test strategy:** extend `test_circuit_breaker.py` (the two new backends fast-fail when open),
`test_resource_governor.py` (eviction backoff on Ollama-down, no 5 s×N stall),
`test_supervisor.py` (enabled-flip log), `test_goal_queue_idempotency.py` (lease-TTL requeue of
a wedged-but-live claim). Mock NVML hang for the Gate-3 timeout.

**Risk/effort:** Low. Mostly mechanical and well-isolated. ~1 day. Can land before EH-2/EH-3 if
desired (independent files).

---

## Ordering & dependencies

```
EH-1 (verification loop)      ← highest leverage, accessibility-critical, land first
EH-2 (durable-failure)        ← destructive path; own sprint; after/parallel to EH-1
EH-3 (store-and-forward)      ← independent; primitives exist
EH-4 (resilience wiring)      ← independent/mechanical; can land any time
```

EH-1 and EH-4 are independent of EH-2/EH-3 (disjoint files), so EH-4 can land first as a
low-risk warm-up. EH-2 and EH-3 both touch `dev_agent.py`/`db.py` lightly — sequence EH-2
before EH-3 to avoid an escalation-table conflict, or keep the EH-3 nudge read-only against the
schema EH-2 introduces.

**Merge against #87:** rebase these on top of #87 (or merge #87 first). E7/E15 specifically
remain open after #87 and are scoped here to complement, not collide with, its circuit-breaker
and heartbeat work.
