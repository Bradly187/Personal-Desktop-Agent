# Clone Sprint Comparison — `D:\Personal-Desktop-Agent` vs `E:\Personal_Desktop_Agent`

**Date:** 2026-06-13
**Baseline:** both trees share git HEAD `d1f05de` on `master`. The D: "Codebase Cleanup and
Orchestration Improvements" sprint exists **only as uncommitted working-tree changes** on top of
that shared baseline (`git -C D: status` shows `core/` and `inference/` staged as deletions from
their flat paths — a `src/` move in progress).
**Method:** every claim below was grep-verified against the actual source in **both** trees, not
inferred from the sprint summary.

---

## TL;DR

The D: sprint is **not ahead of E:** — it is a *diverged, half-finished* working tree that
**removes live code** and **re-implements features E: already has**. Recommendation: **do not merge
it into E: wholesale.** Two of its "dead code" removals are real regressions that its own test run
did not catch. The only change worth keeping is the `kiro_client` prune (already applied to E:).

---

## Section-by-section verdict

| Sprint claim | Reality | Verdict |
|---|---|---|
| Remove `hand_pointer.py` (unused) | In E: it is **live RealSense L515 WIP** — referenced by `scripts/validate_realsense.py` + `tests/test_hand_pointer.py` | ❌ Not dead; deleting breaks WIP |
| Remove `insert_voice_calibration` (db.py) | **Live caller** at `calibration/acoustic_profiler.py:419` | ❌ Regression (see Bug 1) |
| Remove `log_mcp_call` (audit_log.py) | **Two live callers** at `mcp_server/desktop_mcp_server.py:253,259` | ❌ Regression (see Bug 2) |
| Prune `kiro_client` `apply_edit` / `run_terminal` / `get_editor_context` | Genuinely unused in both trees | ✅ Valid — **applied to E:** |
| Add `timeout_s` + `idempotency_key` (tool resilience) | E: **already enforces** both in `core/command_executor.py` (`asyncio.timeout` + SHA-256 idempotency via `tool_calls` / `tool_timeout_config` tables) | ⚠️ Already done here, more completely |
| Add `--speculative-model` vLLM flag | E: **already has** `--speculative` → passes `speculative_model="llama3.1:8b"` to vLLM (`main.py:1346`, roadmap #9) | ⚠️ Already done here |
| Add `PLAN_REGISTRY` orchestration tracing | Absent in E:; but E: already has DAG execution (`_run_dag_waves`) + cross-layer tracing (`monitoring/trace.py`, `trace_id`) | ◐ Genuinely new; overlaps existing capability — evaluate, don't assume superior |
| Move tests `tests/` → `src/tests/`; fix `pytest.ini`, `Path(__file__).parents` | Self-inflicted cleanup of regressions caused by the partial `src/` move | ❌ Inapplicable to E: (flat layout); applying would **break** E: |

---

## Latent bugs introduced in the D: clone (fix these regardless of the merge decision)

Both stem from the same anti-pattern: the `src/` migration was **partial** (`src/storage/db.py`,
`src/storage/audit_log.py` moved; `calibration/` and `mcp_server/` stayed flat), and a method `def`
was deleted while a caller in an *un-migrated* file was left in place.

### Bug 1 — `AgentDB.insert_voice_calibration` removed, caller remains
- **Def deleted from:** `src/storage/db.py`
- **Live caller:** `calibration/acoustic_profiler.py:419` — `self._db.insert_voice_calibration(...)`
- **Impact:** `AttributeError` whenever the AcousticProfiler persists a calibration sample (passive
  voice calibration path).
- **Why tests missed it:** `src/tests/test_acoustic_profiler.py:27` does
  `db.insert_voice_calibration = AsyncMock()` — the test **mocks the method that no longer exists**,
  so it passes green while production crashes.
- **Fix:** restore the `def` (preferred — it is genuinely used), or remove the call site + the
  AcousticProfiler persistence feature.

### Bug 2 — `AuditLog.log_mcp_call` removed, callers remain
- **Def deleted from:** `src/storage/audit_log.py`
- **Live callers:** `mcp_server/desktop_mcp_server.py:263,269` — `await _audit.log_mcp_call(...)`
  (both the error and success audit paths)
- **Impact:** `AttributeError` on **every MCP tool invocation's audit step**.
- **Fix:** restore the `def`, or remove both call sites (loses MCP audit logging — not advised).

### Structural issue — finish or revert the `src/` migration
The tree is currently **inconsistent**: `src/{core,inference,storage,tests,cli}` exist while
`calibration/`, `mcp_server/` (and others) remain flat. This mismatch is the root cause of both bugs
above and of the `ImportError`/`NameError` churn the sprint spent its verification budget on. Either
complete the move (all packages under `src/`, fix every cross-package import) or revert it.

### Verification gap
The sprint validated with **"58 targeted E2E tests."** E: runs **~1,478 collected**. A 58-test subset
that also mocks the very methods that were deleted cannot certify a dead-code removal. Run the full
suite (unmocked at the DB seam) before trusting a pruning pass.

---

## What E: already has (so the clone's additions are redundant here)

- **Per-tool timeouts + idempotency:** `core/command_executor.py` — `_tool_timeout_ms()` +
  `async with asyncio.timeout(...)`, and `_make_idempotency_key()` (SHA-256) with
  `get_tool_call_by_idempotency()` skip-on-duplicate, backed by `tool_calls` / `tool_timeout_config`
  / `tool_cache_config` tables.
- **Speculative decoding:** `main.py` `--speculative` flag → vLLM `speculative_model`.
- **DAG orchestration + tracing:** `inference/dev_agent.py` `_run_dag_waves`; `monitoring/trace.py`
  with `trace_id` threaded through `Command` and a ContextVar.
- **Full flat layout + green ~1,478-test suite** (2 known laptop-off cluster tests excepted).

---

## Recommendations

**For E: (this repo)**
1. ✅ `kiro_client` dead-method prune — **applied** (`get_editor_context`, `apply_edit`,
   `run_terminal`, plus the orphaned `format_editor_context_for_prompt`).
2. Optional: review the clone's `PLAN_REGISTRY` to see if it adds anything over `_run_dag_waves` +
   `trace_id`; only port if it does.
3. Do **not** adopt the `src/` layout or the `pytest.ini` / `Path(__file__).parents` edits.

**For D: (the clone)**
1. Fix Bug 1 and Bug 2 (restore both methods).
2. Finish or revert the `src/` migration — do not leave it half-done.
3. Re-run the **full** suite without mocking the DB seam before trusting the cleanup.
