# Industry-Patterns Gap Analysis — Git, Codebase, Dashboard

**Date:** 2026-07-05
**Scope:** The whole delivery system — git/CI process, codebase architecture, and the observability dashboard — measured against current industry guidance (CI/CD baseline practice, 12-factor/config patterns, supply-chain security for public repos, LangSmith/Langfuse/Grafana-class observability UX, WCAG 2.2).
**Gap ID prefix:** `IG-*` (new prefix; does not collide with prior H/M/C/E/EH/S/GAP/CG/SG/R schemes).
**Relation to prior audits:** This deliberately does *not* re-litigate findings already tracked elsewhere — error-handling gaps E1–E21 (2026-06-16), coding-agent gaps CG-1..9 (2026-07-02), shell gaps SG-1..12 (2026-07-03), or the dashboard-observability spec R1–R8. Overlaps are cross-referenced.

---

## Where the project is *ahead* of industry practice

Worth stating first, because the gap list below is about process scaffolding, not capability:

- **Behavioral eval harness with locked regression baselines** (24 suites, 16 locked baselines, Tier-1 model-free gate in CI, model-backed gate at pre-push). Most production ML teams do not have this.
- **Decision log with mechanical drift enforcement** — `docs/decisions.md` (D001–D028) and CLAUDE.md gotchas are capped by the pre-commit hook. Doc rot is *enforced against*, not just discouraged.
- **Per-trace replay across 5 stores** (`/api/replay/{tid}`: commands + inferences + spans + events + audit) with cost attribution per run — LangSmith-grade data model, self-hosted.
- **Safety architecture**: fail-safe-DENY approval gate, read-only dashboard by design, path sandboxing, content scrubbing before cloud egress, constant-time token comparison.
- **Async discipline**: ~50+ `asyncio.to_thread` sites, 60 Hz loop protection as a written rule, lock discipline, TYPE_CHECKING to break import cycles. No DI framework, but a clean late-binding pattern that serves the same purpose.
- **Spec-driven process with two-gate approval** and 12 prior self-audits.

The dominant theme of the gaps: **the discipline exists but is enforced by convention and client-side hooks rather than by CI/server-side gates.** For a public repo worked by multiple AI assistants, industry guidance says move enforcement to the server.

---

## A. Git & Delivery Process

### IG-1 · Full test suite does not run in CI — **HIGH**
1,206 tests across 231 files exist; CI runs only the 6 eval-harness logic test modules (`evals.yml`). ~93% of tests are mock-based and CPU-safe, so the suite is CI-runnable. Industry baseline: full unit suite on every PR. Today a PR can merge with 1,200 tests red and CI green.
**Fix:** add a `pytest` job (ubuntu, `pip install -r requirements.txt` minus GPU extras, or a curated CI requirements file). Mark hardware-dependent tests with `@pytest.mark.hardware` and deselect in CI. The 3 standalone E2E harness tests are already excluded via root `conftest.py`.

### IG-2 · No lint / format / type-check anywhere — **HIGH**
No ruff, black, or mypy config exists in the repo, yet ~79% of methods carry type hints. That discipline is currently maintained purely by AI-session convention and will erode. Industry baseline: ruff + mypy (or pyright) gating PRs.
**Fix (staged):** (1) `ruff check` + `ruff format --check` in CI — one pyproject block, minutes of work; (2) mypy in non-blocking report mode, promote to blocking per-package as violations hit zero.

### IG-3 · Security enforcement is client-side and bypassable — **HIGH**
The secret scan is a local pre-commit hook (bypass: `DA_SKIP_SECRET_SCAN=1`); model-backed evals gate at a local pre-push hook (skips silently if Ollama is down). Nothing server-side prevents a direct push of a secret to a **public** repo. GitHub provides — free for public repos — secret scanning **push protection**, CodeQL code scanning, and branch protection.
**Fix:** repo settings, no code: enable push protection + CodeQL; add branch protection on `master` requiring `evals.yml` (and the IG-1 pytest job) green. Keep the local hooks as the fast inner loop.

### IG-4 · No automated dependency auditing — **MEDIUM**
75 pinned packages, security rationale kept as hand-written comments in `requirements.txt` (good comments — wrong mechanism). No Dependabot config, no `pip-audit`, no npm audit for `desktop_app/` (Electron 43) or `tts_service/`. Electron especially needs CVE tracking.
**Fix:** `.github/dependabot.yml` covering pip + both npm roots + github-actions; optionally a weekly `pip-audit` CI job.

### IG-5 · No coverage measurement — **MEDIUM**
1,206 tests, zero visibility into what they cover. Industry norm is not necessarily a hard gate, but at least trend reporting.
**Fix:** `pytest --cov` in the IG-1 job with a report artifact; add a ratchet later if desired.

### IG-6 · Swift tests exist but never run — **MEDIUM**
20 XCTest files (4,190 LOC) in `iPadApp/`, and CI *already pays for a macOS runner* to build the IPA — but never executes the tests.
**Fix:** add an `xcodebuild test -destination 'platform=iOS Simulator,...'` step before archive in `build-ipad-app.yml`. Near-zero marginal cost.

### IG-7 · No versioning, tags, or releases — **LOW**
`0.1.0` static in pyproject; zero git tags. For a single-machine deploy this is low urgency, but tags give rollback anchors and let CHANGELOG entries reference an immutable point ("what was running during the June flare week").
**Fix:** tag at merge of each milestone PR (`v0.1.x`); nothing more heavyweight needed.

### IG-8 · Branch and commit hygiene — **LOW**
~15 local branches, several merged or `[origin: gone]` (`feat/chat-context-attachments`, `feat/dashboard-obs-gaps-p1`); `feat/realsense-l515` sits 2 commits ahead unpushed (known: L515 calibration work is uncommitted per memory). Commit style is mostly conventional-commits with lapses (`Update modified files`, e1c0411).
**Fix:** periodic `git branch --merged | xargs git branch -d`; push or stash-tag the L515 work so it can't be lost to a disk event.

---

## B. Codebase & Architecture

### IG-9 · Safety-relevant config file has no schema validation — **MEDIUM**
`core/flags.py` is an industry-grade validated registry (49 flags, startup validation, malformed-value warnings). But `approval_config.json` — which encodes *approval policy per tool*, the safety-critical config — loads as a raw dict. A typo'd key (`"aprove"`) or wrong type silently becomes default behavior. 12-factor/config guidance: validate at the edge, fail loud at startup.
**Fix:** a small pydantic model (or stdlib jsonschema) for `approval_config.json`; refuse to start on unknown keys in the approval-policy section. Same treatment for `skills/manifests/*.json`.

### IG-10 · `storage/db.py` monolith — **MEDIUM**
4,695 LOC, 60 tables, DDL + migrations + all access methods in one file. AGENTS.md #1 makes it the schema source of truth, which is a sound decision — but "source of truth" and "single 4.7 KLOC file" aren't the same requirement. Industry pattern: numbered migration files (even hand-rolled) + per-domain access modules, with the schema still authoritative in code.
**Fix (incremental, no behavior change):** split access methods into `storage/repos/*.py` by domain (runs, goals, calibration, twin, …), keep DDL + `_apply_migrations()` in `db.py` so Rule #1 still points at one file. Do this only with the `changing-the-db-schema` skill loaded, and don't regenerate wholesale (Rule #10).

### IG-11 · Pins without a lockfile or hash-checking — **MEDIUM**
`==` pins are good, but transitive deps still float at install time and there's no `--require-hashes`, so a compromised PyPI upload of a transitive dep installs silently. The runtime env has already drifted once (aiohttp 3.13.5 vs pinned 3.14.1, per 2026-06-20 analysis). Supply-chain guidance for public repos: compiled lockfile with hashes.
**Fix:** `uv pip compile requirements.txt -o requirements.lock --generate-hashes` (or pip-tools); install from the lock. Keeps `requirements.txt` as the human-edited intent file.

### IG-12 · Text logs alongside structured traces — **LOW**
Spans/events/costs are structured in SQLite (excellent), but the log stream itself is plain text. Industry pairs the two (JSON logs keyed by trace_id) so log lines can join the replay timeline. Minor here because replay already covers most needs; note only.

### IG-13 · Flat package layout — **LOW**
No `src/` layout; 12 packages at repo root with an editable install. Works, but risks accidental import of repo-root modules (`conftest.py`, scripts) and makes packaging hygiene depend on careful pyproject excludes. Cosmetic; not worth a disruptive move unless packaging for distribution ever matters.

**Already tracked, intentionally not duplicated here:** durability gaps E3 (saga compensation marked done on exception), E4 (escalation insert loss), E15 (goal lease TTL) remain the most important *code-level* gaps in the repo and predate this analysis — they belong to the E-series backlog, not IG.

---

## C. Dashboard & Observability UI

Context: 19 authenticated routes, 11 read-only panels, live WS activity feed + DAG, trace replay, cost ledger — the *data layer* is genuinely strong. The gaps are presentation-layer, plus one mission-level finding.

### IG-14 · The dashboard itself hasn't had the accessibility treatment the product exists to provide — **HIGH (for this project)**
This is the ironic gap. The product is built for a user with RA, yet the operator UI: hardcodes px font sizes (base 15px, secondary 12.5–13.5px — no `rem`, so browser/OS font scaling doesn't propagate), has touch/click targets at ≥28px (WCAG 2.5.5 recommends 44px, and large targets are precisely what RA flare days need), has `aria-live` only on the chat transcript (dashboard panels update silently for a screen reader), and has no reduced-motion or contrast accommodations. Pain-day adaptation exists for *sensors* (`PainDayEngine`) but not for the *UI the user operates during a flare*.
**Fix:** convert to `rem` with a root font-size variable; raise interactive targets to 44px; `aria-live="polite"` on the alerts panel; a "flare mode" CSS toggle (larger targets/fonts) would be a genuinely novel touch consistent with the product thesis. WCAG 2.2 AA as the reference bar.

### IG-15 · Trace replay renders raw JSON, not a span waterfall — **MEDIUM**
`/api/replay/{tid}` already merges commands, inferences, spans, events, and audit into a time-ordered timeline — but the UI shows it as JSON. Industry agent-observability UX (LangSmith, Langfuse, Jaeger) is a collapsible span tree/waterfall: duration bars, token + cost per node, click-to-expand step I/O. This is pure frontend work over an existing endpoint; the hard part is already done.

### IG-16 · No time-series visualization — **MEDIUM**
VRAM, latency, cloud rate, cost are point-in-time gauges plus session-delta tables. The history exists in `agent.db` (inferences, session_summaries, event_log) but no panel plots it. Grafana-class baseline: sparklines/small multiples for the KPI strip; a VRAM-over-time chart would also make eviction behavior (`vram.evicted`/`restored` events) visible at a glance.

### IG-17 · Polling where push already exists — **LOW**
Alerts poll at ~3s, metrics at ~5s while a WebSocket channel is already open for the activity feed. Extending the `dash_event` frame to carry metric snapshots would cut latency and idle load. Only matters at single-user scale for battery/tidiness; note only.

### IG-18 · Alert lifecycle is view-only — **LOW**
Recent-50 list with active/recovered status, but no acknowledge, no history view beyond the window, no dedup grouping. At this scale, ack is arguably wrong (read-only dashboard is a deliberate safety posture); log-only.

**Cross-references:** IG-16/IG-17 partially overlap the Draft `specs/dashboard-observability-gaps/` R-items; SG-5/6/9/10/11 (shell v1.2 candidates) are adjacent but shell-side. IG-14 and IG-15 are net-new.

---

## Prioritized recommendations

Quick wins (≤1 session each, no architecture change):

1. **IG-1** — pytest job in CI. Biggest single de-risking available.
2. **IG-3** — repo settings only: secret-scanning push protection + CodeQL + branch protection requiring green checks.
3. **IG-4** — `dependabot.yml` (pip + 2 npm roots + actions).
4. **IG-2a** — ruff in CI.
5. **IG-6** — Swift test step in the existing macOS job.
6. **IG-9** — pydantic validation of `approval_config.json`.

Medium-term:

7. **IG-14** — dashboard a11y pass (rem units, 44px targets, aria-live on alerts; consider "flare mode").
8. **IG-2b** — mypy, staged per-package.
9. **IG-15** — span-waterfall UI over `/api/replay`.
10. **IG-11** — compiled lockfile with hashes.
11. **IG-5** — coverage reporting.
12. **IG-16** — KPI sparklines.

Backlog / opportunistic: IG-7, IG-8, IG-10, IG-12, IG-13, IG-17, IG-18.

---

*Method note: three parallel repo surveys (architecture, dashboard, process) on worktree snapshot at commit e1c0411, branch `claude/wonderful-sutherland-d6080c`, 2026-07-05. Facts verified against files cited in the survey outputs; industry baselines: GitHub OSS security guidance, 12-factor config, WCAG 2.2, contemporary agent-observability products.*
