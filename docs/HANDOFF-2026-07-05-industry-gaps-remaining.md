# HANDOFF — Remaining Industry-Pattern Gaps (post-PR #163)

**Date:** 2026-07-05
**For:** any future session (Claude Code or Antigravity) picking up the medium/backlog tier of the 2026-07-05 industry-patterns gap analysis.
**Source analysis:** `docs/audits/2026-07-05-industry-patterns-gap-analysis.md` (IG-1..18, D029)
**Already shipped (do NOT redo):** IG-1, IG-2a, IG-3, IG-4, IG-6, IG-9 — PR #163 (all CI green: full pytest on windows-latest, ruff, Dependabot, Swift simulator tests, approval-config boot gate, push protection + CodeQL + branch protection).

Read this file plus the audit doc and you have everything; no need to re-derive.

---

## Priority 1 — IG-14: Dashboard accessibility pass (HIGH for this project)

**The gap:** the product exists for a user with rheumatoid arthritis, but the operator UI (chat + dashboard at `:8770`) has never had the accessibility treatment the sensors got. Concretely:

| Problem | Where | Target |
|---|---|---|
| Hardcoded px font sizes (base 15px, secondary 12.5–13.5px) — browser/OS font scaling does not propagate | `web_client_chat/style.css` (327 lines, token-based dark theme) | Convert to `rem` with a `--base-font` root variable |
| Interactive targets ~28px (`.tool`, `.approval button`) | `web_client_chat/style.css` | ≥44px per WCAG 2.5.5 — large targets are precisely what RA flare days need |
| `aria-live` only on `#transcript` (`web_client_chat/index.html:21`) | `index.html`, `dashboard.html` | `aria-live="polite"` on the alerts panel at minimum; dashboard panels update silently for screen readers today |
| No reduced-motion / contrast accommodation | `style.css` | `@media (prefers-reduced-motion)`; check token contrast against WCAG 2.2 AA |
| **Flare-mode idea (novel, on-thesis):** a CSS toggle (bigger fonts, 44→56px targets, higher contrast) | new; wire into `dashboard.js` / `chat.js` | `pain_day_score` gauge is ALREADY served by `/api/metrics` (see `monitoring/metrics.py`) — the dashboard can auto-offer or auto-enable flare mode when the score crosses a threshold |

**Constraints:**
- AGENTS.md #5 (pain-day awareness): do not hardcode a flare threshold in JS — read whatever `PainDayEngine`/`BehavioralTwinState.apply_pain_day()` already exposes via the metrics snapshot; if a threshold knob is needed, it belongs in `core/flags.py` (registry, D021) not a magic number.
- AGENTS.md #11 (two-gate): flare-mode auto-switching is feature-level — draft a short spec under `specs/` (`TEMPLATE.md` pattern) and get explicit approval before building. The pure CSS/ARIA remediation (rem, 44px, aria-live, reduced-motion) is hardening of existing UI, not a new feature — prior sessions shipped equivalent scope without a spec.
- The Electron shell (`desktop_app/`) embeds these pages as iframes — rem conversion propagates automatically, but verify the shell's own chrome (tabs, file tree, terminal ≥15px font rule from `specs/desktop-app-shell/`) at the same time.
- No build step exists for `web_client_chat/` (vanilla JS, vendored libs per D023) — keep it that way.

**Verification:** `tests/test_dashboard.py` and `tests/test_chat_workbench_frames.py` cover frame structure, not CSS. Add assertions where feasible (e.g., served HTML contains `aria-live` on the alerts container), then verify visually: `python main.py --chat`, browser zoom 200%, Windows text-scaling 150%, keyboard-only walk of approval card + alerts panel.

---

## Priority 2 — IG-2b: mypy, staged

- No mypy config exists anywhere (ruff shipped in PR #163; `[tool.ruff]` is in `pyproject.toml` — put `[tool.mypy]` beside it).
- ~79% of methods already have return annotations; `from __future__ import annotations` is repo-wide; TYPE_CHECKING blocks are established idiom.
- **Staging plan:** start non-blocking (`continue-on-error: true` job in `.github/workflows/tests.yml`, ubuntu, `mypy --ignore-missing-imports` on ONE package). Promote per-package to blocking as each hits zero. Suggested order (leaf → core): `monitoring/` → `storage/` → `evals/` → `adaptive/` → `inference/` → `core/` (hardest: `hybrid_coordinator.py`, `fusion_engine.py`).
- Windows-only imports (pywin32, comtypes, windows_curses) will need `[[tool.mypy.overrides]] ignore_missing_imports` entries since the lint job runs on ubuntu.

## Priority 3 — IG-15: Span-waterfall trace UI

- Backend is DONE: `GET /api/replay/{tid}` (`core/chat_server.py`, `monitoring/replay.py`) already merges commands + inferences + spans + events + audit into one time-ordered timeline with per-span tokens/cost. The UI renders it as raw JSON.
- Build a collapsible span tree/waterfall (LangSmith/Jaeger style) in `web_client_chat/dashboard.js`: duration bars scaled to the trace window, token + cost per node, click-to-expand step I/O. Pure frontend; no schema or API change.
- Keep it dependency-free (vanilla JS + CSS, D023 precedent — no charting lib).

## Priority 4 — IG-11: Hash-locked requirements

- `uv pip compile requirements.txt -o requirements.lock --generate-hashes` (or pip-tools); keep `requirements.txt` as the human-edited intent file; CI installs from the lock.
- **Session evidence this matters:** `hypothesis` and `boto3` were venv-only drift (broke CI, fixed in #163 by pinning), and unpinned *transitive* numpy behavior differences surfaced the `compute_homography` singular-solve bug. A lock with hashes closes this class.
- Touchpoints when the lock lands: `tests.yml` install step, `evals.yml` minimal-deps step (leave that one curated — it's intentionally tiny), `scripts/backup_agent_state.py` untouched.
- Dependabot (shipped) opens weekly PRs — the lock must be regenerated in those PRs; add a note to `.github/dependabot.yml` or handle in review.

## Priority 5 — IG-5: Coverage reporting

- Add `pytest-cov` to the `tests.yml` pytest job: `--cov=core --cov=inference --cov=storage --cov=monitoring --cov=sensors --cov=adaptive --cov=desktop --cov=evals --cov-report=term --cov-report=xml`, upload `coverage.xml` as artifact. Report-only — no threshold gate until a baseline is known (then ratchet).

## Priority 6 — IG-16: KPI sparklines

- Data already in `agent.db`: `inferences` (latency/tokens/cost over time), `session_summaries` (`monitoring/trends.py`), `event_log` (`vram.evicted`/`vram.restored` events for a VRAM-over-time strip).
- Smallest useful slice: inline SVG sparklines in the "Now" KPI strip (`dashboard.js`), fed by a new `?series=1` param on `/api/metrics` or a small `/api/series` endpoint reading existing tables (`asyncio.to_thread`, read-only — follow the existing `_api_*` handler pattern in `core/chat_server.py`).
- Overlaps Draft spec `specs/dashboard-observability-gaps/` — reconcile with its R-items rather than forking.

---

## Backlog (opportunistic, low)

- **IG-7 versioning:** start tagging master at milestone merges (`v0.1.x`). No tags exist as of 2026-07-05.
- **IG-8 branch hygiene:** delete merged/`gone` locals (`feat/chat-context-attachments`, `feat/dashboard-obs-gaps-p1`, more via `git branch --merged`). **`feat/realsense-l515` is 2 commits ahead, unpushed, and holds the L515 calibration work — push it before pruning anything.**
- **IG-10 db.py split:** move access methods to `storage/repos/*` by domain; keep DDL + `_apply_migrations()` in `storage/db.py` so AGENTS.md #1 still points at one file. Load `.agents/skills/changing-the-db-schema` first; do NOT regenerate wholesale (Rule 10).
- **IG-12 structured logs, IG-13 src/ layout, IG-17 WS-push metrics, IG-18 alert ack:** noted in the audit; none worth a dedicated session.
- **TestFlight trigger (new, from this session):** `build-ipad-app.yml` uploads to TestFlight on ANY push touching `iPadApp/**` — including feature branches (condition at the `Upload to TestFlight` step: `github.event_name == 'push'`). If branch uploads are unwanted, gate on `github.ref == 'refs/heads/master'`.
- **Branch protection follow-up:** master currently requires only `evals-tier1`. Once `pytest`/`lint`/`test` prove stable across a few PRs, add them to required checks: `gh api -X PUT repos/Bradly187/Personal-Desktop-Agent/branches/master/protection` (current config: strict=false, enforce_admins=false).

---

## Gotchas learned in the PR #163 iteration (save yourself the CI round-trips)

1. **Swift tests:** all 20 XCTest classes are now `@MainActor` (app types are MainActor-isolated). New test files touching app types need the same annotation.
2. **`SettingsStore` treats a stored 0.0 as "unset"** (`.nonZero ?? default` idiom, e.g. `tiltDeadZone` → 1.5°). In tests, disable knobs on the live instance (`settings.tiltDeadZone = 0.0`), never via `suite.set(0.0, …)`.
3. **FP boundaries in tests:** 0.001 and 0.2 aren't binary-representable; construct at-threshold values with `nextUp`/`nextDown` loops (see `MessageSuppressionTests`, `StationaryLockTests`).
4. **The Swift test job's log filter hides compile errors** — full output is uploaded as the `xcodebuild-test-log` artifact on failure; download that instead of squinting at the filtered job log.
5. **Environment-dependent tests are the enemy:** anything depending on `AWS_BEARER_TOKEN_BEDROCK`, a repo `.venv`, or a specific numpy build passed locally for months and failed on the first clean runner. Pattern: stub credentials with `patch.dict("os.environ", …)`, skip on missing local infra with a clear reason, and make numerical contracts explicit (`compute_homography` cond-check).
6. **`gh pr checks --watch` can return a stale rollup** right after a push; poll `commits/{sha}/check-runs` instead.
7. **pytest job quirk:** teardown prints `RuntimeError: Event loop is closed` noise on Windows (aiohttp + proactor); it is not a failure signal — read the `short test summary info` section.
