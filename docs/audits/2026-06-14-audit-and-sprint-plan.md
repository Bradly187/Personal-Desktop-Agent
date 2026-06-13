# Codebase Audit + Sprint Plan — 2026-06-14

Source: four parallel subsystem audits (concurrency-core, inference/routing, security/ops,
storage/proactivity) over `core/`, `inference/`, `storage/`, `skills/`, `adaptive/`.
Two HIGH "thread-boundary" findings were manually re-verified and **downgraded to MEDIUM**
(all callers are on-loop today — fragile invariant, not an active bug). Gate-3 NVML was
verified as a **real** latency defect.

Branch context: master @ `0a95421`, tree clean, no open PRs.

---

## 1. Audit findings (record)

Severity after verification. `*` = manually re-verified by the orchestrator.

| # | Sev | Class | Location | Issue | Fix direction |
|---|-----|-------|----------|-------|---------------|
| 1 | HIGH | correctness | `storage/db.py:1765` `enqueue_goal` | `INSERT OR IGNORE` on `UNIQUE(idempotency_key)` makes re-enqueue a no-op even after the row reaches `done/failed/cancelled` — recurring/event goals silently never re-run; caller gets the stale terminal row id. | On collision, if existing status is terminal, UPDATE → `queued`; else scope the key with an occurrence/time component. |
| 2 | HIGH | concurrency | `storage/db.py:1907` `promote_due_goals` + `core/proactive_scheduler.py:115` | Recurrence re-lay (`enqueue_scheduled_goal`) passes **no** idempotency_key; overlapping promotion / crash-restart accumulates duplicate `scheduled` rows. No UNIQUE on (goal, execute_at). | Pass deterministic `idempotency_key=f"{source_trigger}:{goal}:{int(nxt)}"`. |
| 3 | HIGH | security | `core/goal_session.py:169` `_path_in_scope` | Uses `os.path.abspath` (lexical) not `realpath`; a junction/symlink in the repo root escapes the writable-root allowlist for WRITE_FILE + RUN_TERMINAL cwd. `..` and case are handled; symlink/junction is not. | `os.path.realpath` before the prefix compare. |
| 4 | HIGH | security | `adaptive/content_filter.py:102` `_PATTERNS` | Outbound skill-send scrub misses Google OAuth refresh tokens (`1//…`), API keys (`AIza…`), and generic `Bearer …`. The Gmail skill that egresses Google data is live. | Add the three patterns. |
| 5 | HIGH | correctness | `inference/dev_agent.py:962` `_replan` | Recovery replan calls `_parse_plan` (regex) only, never `_parse_plan_json`; a JSON-format recovery plan parses to zero steps → spurious halt/escalation. | Try `_parse_plan_json` first, regex fallback (mirror `_plan_and_run_locked`). |
| 6 | HIGH | latency | `core/hybrid_coordinator.py:1533` Gate 3 | `nvmlInit/GetMemoryInfo/nvmlShutdown` run synchronously on the event loop (2s TTL cache); every miss stalls all concurrent accessibility/voice tasks. `model_router._free_vram_gb` shares the issue. | Background VRAM poller refreshes a cached value; Gate 3 reads cache only. Or `to_thread`. |
| 7 | MED | correctness | `core/email_watcher.py:118` | Dedup `_seen` is in-process only; every restart re-baselines and absorbs (drops) unread that arrived during downtime. | Persist last-seen id set / high-water mark. |
| 8 | MED | correctness | `storage/db.py:821` `_migrate` | Bumps `user_version` after the batch even if an ALTER genuinely failed (caught+logged) → broken column never retries. | Only bump version if every ALTER succeeded or was duplicate-column. |
| 9 | MED | concurrency | `storage/db.py:1839` `requeue_stale_running` | No lease/instance guard; if run alongside a live drainer it flips an executing goal back to `queued` → two runners on one goal. | Gate to pre-drainer startup only, or add `owner_pid`+`claimed_at` lease and requeue only stale leases. |
| 10 | MED | correctness | `core/event_rule_engine.py:34` `_get_path`/`_eval_predicate` | Returns `None` for both missing field and JSON `null`; `exists` and `eq null` misfire. | Use a `_MISSING` sentinel distinct from `None`. |
| 11 | MED | security | `core/goal_session.py:106` `_strip_quoted` | Strips double-quoted spans before the dangerous-op scan, but bash *expands* `$()`/backticks inside double quotes → `echo "$(rm -rf x)"` is allowlisted. | Strip only single-quoted spans; leave double-quoted for the scan. |
| 12 | MED | ops | `scripts/backup_agent_state.py:291` `restore_backup` | Writes to absolute `dest` from the in-zip manifest; `..` guard checks archive paths only, never `dest`. Foreign/tampered archive overwrites arbitrary absolute paths. | Validate each `dest` resolves under {project_root, claude_home, backup_root}; reject otherwise. |
| 13 | MED | weak-logic | `core/crash_marker.py:28` | 2-instance false positive (known): no PID check before declaring a crash; concurrent start mis-reports + deletes the marker under a live instance. | Write PID; verify recorded PID dead (`os.kill(pid,0)`/psutil) before declaring a crash. |
| 14 | MED | correctness | `inference/dev_agent.py:909` `_run_dag_waves` | Two same-path `WRITE_FILE` with empty `after` run concurrently → nondeterministic last-writer; planner independence unverified. | Detect duplicate WRITE_FILE target in a wave → demote to barrier (serialize). |
| 15 | MED | latency | `inference/dev_agent.py` replan/retry + `model_router` | No backoff between read-only retry and the two `MAX_REPLANS` 30B re-invocations; transient outage → tight retry→replan→retry. | Short backoff; short-circuit a replan whose first step is byte-identical to the failed step. |
| 16 | MED | concurrency | `core/circuit_breaker.py:78` half-open | A lost probe whose outcome arrives late can re-open the breaker against a newer in-flight probe (generation conflation). | Tag probes with a generation id; ignore stale-generation outcomes. |
| 17 | MED | weak-logic | `core/rate_limiter.py:97` | On exhausting bounded retries the loop returns True (fail-**open**) admitting token-free → transient rate overshoot under contention. | Return False (drop) or loop to a hard wall-clock deadline. |
| 18 | MED | latency | `core/hybrid_coordinator.py:1638` + `cloud_dev_agent.py:238` | `RateLimiter.check("anthropic")` is awaited *outside* the 10s `asyncio.timeout` breaker → a saturated bucket stalls past the advertised budget. | Move `check()` inside the timeout window, or bound the wait. |
| 19 | MED | concurrency | `core/resource_governor.py:163` `notify_pain_day_change` `*` | `asyncio.create_task` from a sync method — works (all callers on-loop) but the invariant is undocumented/unenforced. **Downgraded from HIGH.** | Capture loop at `start()`; `call_soon_threadsafe`-guard for non-loop callers; document. |
| 20 | MED | concurrency | `core/scheduler.py:230` `submit` `*` | `asyncio.get_event_loop()` for the future; fragile under a non-loop caller / py3.12. **Downgraded from HIGH.** | `get_running_loop()`; document loop-thread-only. |
| 21 | MED | concurrency | `core/scheduler.py:362` `_worker` | If `create_task` dispatch throws after `queue.get()`, the dequeued `future` is never resolved and the coro leaks → awaiter hangs. | On dispatch failure `future.set_exception` + `coro.close()`. |
| 22 | LOW | correctness | `core/supervisor.py:191` | Sliding-window restart budget permits an indefinite slow crash-loop just under the window edge. | Add a total-restart ceiling or exponential backoff. |
| 23 | LOW | security | `storage/audit_log.py:47` (M1) | Triggers block UPDATE/DELETE but no hash-chaining and no DROP-TABLE/DROP-TRIGGER protection → not tamper-evident. | Per-row `prev_hash` SHA-256 chain. |
| 24 | LOW | routing | `core/domain_classifier.py:175` | Dev domains only scored at `word_count>=4`; terse "debug this test" / "prove lemma 2" fall to `general` → wrong specialist, no RAG. | High-signal-keyword override regardless of length. |
| 25 | LOW | weak-logic | `core/hybrid_coordinator.py:1843` `_parse_params` SCROLL | Amount parse is unbounded (negative/huge ints pass to pyautogui); mis-greedy on numeric targets. | Clamp 1–20; accept only positionally after the direction. |
| 26 | LOW | weak-logic | `core/hybrid_coordinator.py:268` `_apply_vocabulary_corrections` | Substring `replace` corrupts legitimate words (e.g. "tightly"→"typely"). | Word-boundary (`\b`) match. |
| 27 | LOW | security | `inference/dev_agent.py:1585` SEARCH_WEB | `query.replace(' ','+')` not URL-encoded; `&`/`#`/quotes malform the URL. | `urllib.parse.quote_plus`. |
| 28 | LOW | latency | `storage/personal_kb.py:300/369` | Redundant `count()` thread-hop before every `query`; one large PDF page / big `col.add` is uninterruptible by `_stop_event`. | Skip the count probe; check `_stop_event` between PDF pages. |
| 29 | LOW | correctness | `adaptive/continuous_trainer.py` record_failure | A dev-agent failure writes a `command`-domain counterexample regardless of namespace. | Thread namespace/domain into `upsert_few_shot_counterexample`. |
| 30 | LOW | correctness | `inference/dev_agent.py:2161` `_git_commit` | `git add -u` raises `CalledProcessError` (not the `RuntimeError` the saga expects); user sees a raw repr, not git stderr. | Capture output, raise `RuntimeError(stderr)`. |

### Verified healthy (no defect)
C1 pairing-token before `ws.prepare` (`hmac.compare_digest`, no TOCTOU); watchdog `/d /s /c`
quote fix correct; backup `.tmp`→rename atomic; `claim_next_goal` single-consumer via drain
lock; `record_failure` counters never touch PreferenceModel/SemanticMemory/positive few-shot;
malformed-LLM output → CLARIFY throughout; destructive ops fail-safe to DENY on
silence/ambiguity/timeout; `_parse_plan` regex superseded by JSON-schema parse on the main path.

---

## 2. Sprint O — Proactivity & Storage Correctness  *(recommended first)*

**Goal:** make the proactivity tier survive process restart and repeated occurrences;
eliminate silent goal-loss and recurrence storms. Closes the known store-and-forward gap.

**Findings:** #1, #2, #7, #8, #9, #10 (+ #16 event-cooldown burst from `event_rule_engine`).
Optionally prepend **#4** (Google-scrub) if Gmail is actively in use — it's a one-file,
~10-line fix and it's leaking now.

**Files & changes:**
- `storage/db.py`
  - `enqueue_goal` (#1): on `UNIQUE(idempotency_key)` collision, `SELECT status`; if terminal → `UPDATE … SET status='queued', execute_at=?` and return that id; else no-op as today. Add a test matrix over all statuses.
  - `promote_due_goals` / `enqueue_scheduled_goal` (#2): thread a deterministic `idempotency_key` through the re-lay; add `UNIQUE(goal, execute_at, source_trigger)` index or rely on the key. Migration bump (next `user_version`).
  - `_migrate` (#8): wrap each migration step; only advance `user_version` when the whole batch applied (treat `duplicate column` as success, any other `OperationalError` as failure → leave version, log, retry next boot).
  - `requeue_stale_running` (#9): add `owner_pid`+`claimed_at` columns (migration); `claim_next_goal` stamps them; `requeue_stale_running` requeues only rows whose `owner_pid` is dead or `claimed_at` older than a lease TTL. Keeps single-consumer guarantee even if a second instance starts.
- `core/proactive_scheduler.py`
  - `next_occurrence` interval branch: advance from intended `execute_at` (`prev + every_s` looped to > now), not `now` — removes phase drift after downtime. DST: build daily candidates from local wall-clock components, not raw timestamp arithmetic.
- `core/email_watcher.py` (#7): persist the seen-id set (small table or a JSON sidecar under `~/.claude/`); on start, load it instead of re-baselining; baseline only on true first-ever run. This is also the store-and-forward fix.
- `core/event_rule_engine.py` (#10, #16): `_MISSING` sentinel in `_get_path`; update `last_fired_at` / an in-memory next-allowed map *before* awaiting `_fire` so a same-tick burst is suppressed.

**Test strategy:** `tests/test_goal_queue_idempotency.py` (re-enqueue across every terminal
state; duplicate-occurrence collapse), extend `test_proactive_scheduler.py` (interval drift
after simulated downtime, DST boundary, no double-promote), `test_email_watcher.py`
(restart resumes dedup, downtime mail not dropped), `test_event_rule_engine.py`
(missing-vs-null predicate, burst cooldown). Migration round-trip test (fresh DB + upgrade
from prior `user_version`). Run the full suite (~1430 baseline, 2 known laptop-off fails).

**Risk/effort:** Medium. The migration + new columns are the riskiest part — guard with a
fresh-DB and an upgrade-path test. Tightly scoped to `db.py` + 3 small files. ~1–2 days.

---

## 3. Sprint P — Security Residuals

**Goal:** close the sharp edges on the Sprint-N gates. The Google-scrub gap is the most
time-sensitive (live egress).

**Findings:** #3, #4, #11, #12, #13. Optional: #23 (audit hash-chain, closes M1).

**Files & changes:**
- `adaptive/content_filter.py` (#4): add patterns — Google API key `AIza[0-9A-Za-z\-_]{35}`,
  OAuth refresh `1//[0-9A-Za-z\-_]+`, generic `Bearer\s+[A-Za-z0-9\-._~+/]+=*`. Unit-test each
  against a synthetic skill payload.
- `core/goal_session.py`
  - `_path_in_scope` (#3): `realpath` both the candidate and each scope root before
    `commonpath`/prefix compare. Add a junction-escape test (create a junction in a tmp repo
    pointing outside; assert denied). Mirror the same fix anywhere `command_executor` resolves
    the writable-root (it reuses `_path_in_scope`, so one fix covers both).
  - `_strip_quoted` (#11): strip only single-quoted spans; double-quoted spans stay in the
    string for `_DANGEROUS_SHELL_RE`. Test `echo "$(rm -rf x)"` → denied, `echo 'literal $('` → still allowed.
- `scripts/backup_agent_state.py` `restore_backup` (#12): resolve each manifest `dest` and
  assert it's under an allowed root set; on violation, skip + warn (don't abort the whole
  restore). Test with a crafted manifest pointing at `C:\Windows\…`.
- `core/crash_marker.py` (#13): write `{pid, started_at}` into `agent.running`; on
  `check_and_mark`, if the recorded pid is still alive treat as concurrent instance (no crash
  claim, don't delete the other's marker). Test: stale-pid → crash; live-pid → concurrent.
- *(optional)* `storage/audit_log.py` (#23): per-row `prev_hash = sha256(prev_row || payload)`;
  a `verify_chain()` helper. Doesn't stop a DROP but makes truncation detectable.

**Test strategy:** `tests/test_content_filter.py` (the 3 new token classes + no false-positive
on benign text), `tests/test_goal_session_scope.py` (junction/symlink escape, double-quote
subshell), `tests/test_backup_restore.py` (foreign-dest rejection), `tests/test_crash_marker.py`
(pid liveness branches). All Windows-path-aware.

**Risk/effort:** Low–Medium. Mostly pattern/validation; the junction test needs a real
junction on Windows (`mklink /J` via subprocess in a tmpdir). ~1 day.

---

## 4. Sprint Q — Hot-path Latency & Runtime Robustness

**Goal:** remove the one real loop-blocking latency source and harden the runtime invariants
the audit flagged as fragile.

**Findings:** #6 (+ model_router VRAM), #5, #14, #15, #17, #18, #19, #20, #21, #16, #22.

**Files & changes:**
- `core/hybrid_coordinator.py` Gate 3 + `core/vram.py`/`model_router.py` (#6): add a small
  background VRAM poller (1–2s) that owns `nvmlInit` once at start and `nvmlShutdown` at stop,
  publishing a cached free-GB; Gate 3 and `_free_vram_gb` read the cache (never call NVML on
  the loop). Reuse the existing `core/vram.py` signal as the single source.
- `inference/dev_agent.py`:
  - `_replan` (#5): `_parse_plan_json` first, regex fallback.
  - `_run_dag_waves` (#14): within a wave, detect duplicate WRITE_FILE target paths → serialize
    (treat as implicit dependency / demote to barrier).
  - replan/retry (#15): short backoff (e.g. 0.5–1s) before the read-only retry and between
    replans; skip a replan whose first step is byte-identical to the failed step.
  - `_git_commit` (#30, cheap): capture output, raise `RuntimeError(stderr)`.
- `core/resource_governor.py` (#19) + `core/scheduler.py` (#20, #21): capture the loop at
  `start()`; `notify_pain_day_change` uses `call_soon_threadsafe` when off-loop; `submit` uses
  `get_running_loop`; `_worker` resolves/closes an orphaned future+coro on dispatch failure.
  Document the loop-thread-only invariant.
- `core/circuit_breaker.py` (#16): probe generation id; ignore stale-generation outcomes.
- `core/rate_limiter.py` (#17): fail-**closed** (drop) on exhausted retries; and move the
  `anthropic` `check()` inside the 10s timeout in `hybrid_coordinator`/`cloud_dev_agent` (#18).
- `core/supervisor.py` (#22): add a total-restart ceiling alongside the sliding window.

**Test strategy:** `tests/test_gate3_vram_cache.py` (NVML called off-loop / never on the tick;
cache freshness), extend `test_dev_agent*` (JSON recovery plan parses; same-path writes
serialized; replan backoff/dedup), `test_circuit_breaker.py` (stale probe ignored),
`test_rate_limiter.py` (fail-closed), `test_scheduler.py` (orphaned-future resolved),
`test_supervisor.py` (slow crash-loop eventually latches). A microbench asserting Gate 3
adds < 1ms on the loop on a cache hit.

**Risk/effort:** Medium–High (touches the most files; the VRAM poller is the only structural
change). Lower urgency than O/P. ~2 days.

---

## 5. Backlog (fold in when the file is already open)

#22 supervisor (if not in Q), #24 terse-dev-query override, #25 SCROLL clamp, #26 vocabulary
word-boundary, #27 SEARCH_WEB urlencode, #28 personal_kb count-probe + PDF-page cancel,
#29 counterexample namespace tag, gate-2 token-gate privacy exemption.

## 6. Recommended order
1. **Sprint O** — live user-facing correctness, tight scope, closes store-and-forward. *(Prepend #4 if Gmail is in use.)*
2. **Sprint P** — security residuals; #4 is the most time-sensitive single fix.
3. **Sprint Q** — latency + robustness; broadest, lowest urgency.

Each sprint is independently shippable as its own PR off master.
