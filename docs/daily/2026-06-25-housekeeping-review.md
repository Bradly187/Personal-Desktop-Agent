# Daily Review + Housekeeping — 2026-06-25

*Automated session (scheduled `daily-code-review-housekeeping`). Covers yesterday's
work (2026-06-24) and today's housekeeping audit of the repo.*

`master` tip at audit time: **`565ff93`** — `obs: close P1-P6+P8 observability gaps`.
Working tree clean. No commits dated 2026-06-25 yet.

---

## 1. Yesterday's work (2026-06-24)

Four distinct pieces of work landed yesterday. **Only two reached `master`; the other
two are still on open branches** (see §2 — this is the headline housekeeping finding).

| Work | Commit / PR | State |
|------|-------------|-------|
| Kokoro TTS default-backend regression gate | `c574059` (merge) / `e425acd` | **Merged to master** |
| TTS current-state doc refresh | `808492d` (merge) / `e1824c0` | **Merged to master** |
| Observability gaps P1–P6 + P8 | `565ff93` | **Merged to master** |
| Docs truth-up (diagrams + counts) | `6d28f24` / **PR #132** | **OPEN — not in master** |
| Flip `DA_CRITIC`/`DA_TESTER`/`DA_PLAN_REPAIR` defaults ON | `60bef56` / **PR #133** | **OPEN — not in master** |

### 1a. Observability gaps (`565ff93`, on master)
The substantive code shipped to master yesterday. Closes audit gaps P1–P6 + P8:
- **P1** — `mcp_server/tools/*` previously had zero logging; now emit debug/warning.
- **P2** — `core/domain_classifier.py` logs winner + runner-up on every call.
- **P3** — targeted `try/except` + warnings on `pyautogui` failures in mouse/keyboard/screen tools.
- **P5** — `TraceRecorder.flush_all()` persists the ring buffer on graceful shutdown;
  `dump_to_file()` writes a JSON crash dump via an `atexit` handler in `main.py`.
- **P6** — `GET /api/session-live`: live KPI rollup for the current session (no DB write).
- **P8** — **new module** `monitoring/metric_watcher.py` (`MetricWatcher`, 169 LOC): a
  background coroutine with edge-triggered hysteresis alerts →
  `metric.threshold_crossed` EventBus topic, wired into `main.py` alongside the watchdog.
  9 new tests (`tests/test_metric_watcher.py`).

Verified clean this session: all touched files `py_compile` OK, no `TODO`/`FIXME`/`HACK`.

### 1b. Kokoro TTS gate (on master)
Regression gate locking Kokoro as the default `tts_backend` (matches the 2026-06-23
Kokoro merge). Plus a current-state doc refresh for the Kokoro switch.

### 1c. Truth-up (PR #132, **open**) and flag-flip (PR #133, **open**)
Cut from master earlier on 2026-06-24, before the obs commit. Neither is merged.
Details and risk in §2.

---

## 2. Housekeeping audit (2026-06-25)

### Finding A — three doc/flag branches are mid-flight and unmerged
The doc staleness that a naive sweep would "fix" is **already fixed in open branches**.
Editing `master` to fix the same lines would collide with these on merge
(AGENTS.md #8 — don't duplicate/conflict with other sessions' work). Left untouched:

| Branch / PR | What it fixes | Verified vs code today |
|-------------|---------------|------------------------|
| `docs/truth-up-2026-06-24` / **#132** | `CLAUDE.md` schema fact **42→48 tables**; WS counts 25/6 → **26/12**; diagram gaze/head/sound scrub; trajectory gotcha | **Correct.** `storage/db.py` = 48 `agent.db` tables (53 `CREATE TABLE` − 2 prose false-positives − 3 DuckDB `benchmark_*`). |
| `feat/flip-eval-gated-flags` / **#133** | `DA_CRITIC`/`DA_TESTER`/`DA_PLAN_REPAIR` docs + `dev_agent.py` defaults OFF→ON | **Consistent.** On master these are still coded *and* documented OFF — #133 flips both together, so master is internally consistent today. |
| `docs/changelog-catchup-2026-06-22` (`9752b41`) | Backfills `CHANGELOG.md` for 2026-06-18→22 merges; bumps status date | Backfills the gap noted in Finding C. |

> **Action for the human:** merge #132, #133, and the changelog-catchup branch.
> Until then `master`'s `CLAUDE.md` reads "42 tables" / "flags OFF" — stale relative to
> the *branches*, but **internally consistent** with master's own code, so not dangerous.

### Finding B — the obs work is undocumented (the real uncovered gap)
`565ff93` shipped to master at 17:15 yesterday, **after** all three branches in Finding A
were cut, so none of them mention it. `monitoring/metric_watcher.py`, the
`metric.threshold_crossed` topic, `GET /api/session-live`, and
`TraceRecorder.flush_all()`/`dump_to_file()` appear in **no** doc (`CLAUDE.md`,
`docs/CHANGELOG.md`, architecture docs all silent).

> **Recommendation:** fold an obs entry into `CLAUDE.md` (Architecture/observability)
> and `CHANGELOG.md` as part of the *next* truth-up pass — ideally rebased onto #132 so
> all `CLAUDE.md` edits land in one commit rather than racing master. Not fixed inline
> here to avoid a third competing edit to `CLAUDE.md` while #132/#133 are open.

### Finding C — CHANGELOG.md lags ~10 days behind master
`docs/CHANGELOG.md`'s newest entry is 2026-06-14/15 (PRs #62–#71). Missing from master's
CHANGELOG: self-skilling macros (#131, merged 06-22), Kokoro TTS (06-23), the obs gaps
(06-24), and the 06-18→20 sprint work. The `changelog-catchup-2026-06-22` branch covers
06-18→22 but not 06-23/24 and is itself unmerged. Covered by the Finding A merge action
plus the Finding B obs entry.

### Finding D — `docs/daily/` coverage gap
Last substantive daily review is **2026-06-07**; the only file since is a 280-byte stub
(`2026-06-19-sprint-3-accrual.md`). The heavy 06-08→06-24 run (edit-format ACI, plan
contract, critic/tester, WSL routing, self-skilling, Kokoro, obs) has no day-by-day
notes. This document partially closes the tail; the bulk history lives in `CHANGELOG.md`
and the auto-memory index, so backfilling old dailies is low priority.

### Cleanup performed this session
- **Removed** stray `node_out.log` at repo root — a gitignored, untracked runtime log
  from 2026-05-15 referencing the retired Polly **"Voice=Ruth"** TTS path (obsolete since
  the Danielle→Kokoro migration). Gitignored, so no repo impact; local-folder hygiene only.

### Checked and healthy (no action)
- All relative `.md` links in `CLAUDE.md` resolve (`docs/file-map.md`, `docs/tts.md`, etc.).
- `agent.db`, `analytics.duckdb`, `audit.db`, virtualenvs, caches — all correctly gitignored.
- Obs-commit files compile clean; no stray `TODO`/`FIXME`/`HACK` introduced.
- `feat/self-skilling-macros` is **merged** (#131); the remaining `feat/realsense-l515`
  branch is the paused L515 head-pointer work (camera-positioning blocker — unchanged).

---

## 3. Open actions (for the human, in priority order)
1. **Merge PR #132** (truth-up) and **PR #133** (flag-flip) — resolves Findings A & most of C.
2. **Merge `docs/changelog-catchup-2026-06-22`** — resolves the rest of Finding C.
3. **Add an observability entry** for `565ff93` to `CLAUDE.md` + `CHANGELOG.md`
   (Finding B) — best done rebased onto #132 to avoid a competing `CLAUDE.md` edit.
4. Resume `feat/realsense-l515` only after the camera-positioning blocker is cleared
   (unchanged from the 2026-06-23 handoff).
