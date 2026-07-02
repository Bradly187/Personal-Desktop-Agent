# Session Handoff — Claude Code → Antigravity (2026-07-02)

**Audience:** the next Antigravity session on this repo.
**How to use:** AGENTS.md (you read it natively) is authoritative for behavior;
CLAUDE.md for deep context. This document is the *delta* — exact repo state,
today's work, loose ends, and operational facts that live outside the repo.

---

## 1. Repo state right now

- Branch: `master` @ `dcffc93` (all PRs merged; **no open PRs**).
- **One uncommitted change:** `adaptive/content_filter.py` (+90/−48). Verified
  line-by-line: it is a **formatting-only** pass (black-style quote/wrap changes,
  import reorder, unused `field` import removed) — zero functional delta.
  Commit it as a `style:` chore or `git checkout --` it; don't let it ride along
  inside an unrelated feature commit.
- `agent.db` schema: `PRAGMA user_version = 8` — unchanged by today's work
  (`storage/db.py` is the only source of truth, Rule 1).

## 2. What shipped today (2026-07-02, chronological)

| Ref | What |
|---|---|
| PR #157 (merged) | 8 concern-focused Python class diagrams → `specs/ipad-sensor-focus/diagrams/16-python-class-diagrams.md`; `02-class-diagram.md` retired to Swift-only + pointer; index + CLAUDE.md count updated. |
| PR #158 (merged) | **Coding-agent gap analysis** → `docs/audits/2026-07-02-coding-agent-gap-analysis.md`. PDA vs Claude Code / Codex / Antigravity (July-2026 state). Defines the CG-1..CG-9 gap register. This is the roadmap-shaping artifact. |
| `b268ee1` (master) | **CG-1 + CG-2 implemented**: `core/egress.py` `EgressController` (rejects RFC-1918/loopback + non-http(s), wired into `DevAgent._fetch_url` and `inference/sandbox.py`); `VoiceRewindHandler` (voice: "undo that run" / "undo run", via `voice_system_control.py`) with `promote_checkpoints_to_pending` in `storage/db.py` so *successful* runs keep rollback checkpoints; **flipped `DA_REPO_CONTEXT` + `DA_DELEGATE` ON** and re-locked the `dev_trajectory` eval baseline. Specs: `specs/dev-agent-egress-controls/`, `specs/voice-invokable-rewind/`. Tests: `tests/test_dev_agent_egress.py` + saga-test updates. |

## 3. CG register status (see the audit doc for full detail)

- **CG-1 egress controls — DONE** (`b268ee1`).
- **CG-2 voice rewind — DONE** (`b268ee1`).
- **Activation debt — CLEARED** for `DA_REPO_CONTEXT` / `DA_DELEGATE` (now ON, baseline re-locked). `DA_SESSION_MEMORY` stays OFF — precondition still unmet (D014).
- **Remaining, in recommended order:**
  - **CG-3** trajectory compaction-on-overflow (highest value; eval-gate via `dev_trajectory`; NOT the same mechanism as `DA_TRAJECTORY_REDUCE`, which stays OFF per D011 — don't resurrect it by accident)
  - **CG-4** deny-only local adjudicator for queued escalations (may only DENY/annotate, never APPROVE — preserves fail-safe DENY, Rule 4)
  - **CG-5** post-run walkthrough artifact + TTS summary
  - **CG-6** scheduled tasks over the existing goal queue
  - **CG-7** plan-preview voice gate for large plans
  - **CG-8** WorkflowRunner `pipeline` mode (already specced: `specs/workflow-orchestration/`) + best-of-N plan candidates
  - **CG-9** SKILL.md as the rung-3 self-skilling format (when rung 3 is revisited; rung 4 remains forbidden, D010)
- **Every CG item requires the two-gate flow (Rule 11):** Draft spec → explicit human approval → `In Progress` → code. Nothing is pre-authorized.

## 4. Loose ends / landmines

1. **Spec Status drift:** both `specs/dev-agent-egress-controls/requirements.md`
   and `specs/voice-invokable-rewind/requirements.md` still say
   `Status: In Progress` although the implementation is merged. Promote them per
   the TEMPLATE.md lifecycle on the next doc pass.
2. **Branch `claude/quizzical-ptolemy-1b18b1` holds unmerged work** (2026-07-01
   systems-analysis session): chat token auth (D020), `core/flags.py` central
   flag registry (D021), stub-signature guard, `model.downgraded` event,
   `sensor_telemetry.trace_id` (**bumps schema to v9** — use
   `.agents/skills/changing-the-db-schema` when merging). It also carries fixes
   for **two pre-existing master test failures** (dashboard-obs mutation routes;
   goal_queue `_locked_body` TypeError) — if you run the full suite on master,
   expect those two, they're known (verify, state is from 2026-07-01).
3. **gh merge gotcha:** before `gh pr merge` on anything stacked, check
   `baseRefName` — a PR based on an undeleted feature branch merges into THAT
   branch, not master (the #152 incident; fixed via #155).
4. venv has an orphaned `chatterbox-tts 0.1.7` (backend removed in #154) —
   harmless; `pip uninstall chatterbox-tts` when convenient.
5. Uncommitted `feat/realsense-l515` work exists on its branch (L515 head-pointer
   calibration, paused pending tripod) — don't touch unless resuming that thread.

## 5. Operational facts not derivable from the repo

- The agent runs **fully local**; iPad connects via manual IP `192.168.18.2`.
- Launcher lives at `E:\PDA_launcher\`; logon task has a 1-min delay because the
  ReFS `E:` drive mounts after `-AtLogOn` fires.
- **Two `python.exe main.py` processes are normal** (Windows venv launcher
  parent + worker) — one agent; don't kill the parent.
- Laggy typing on this machine = Logitech G HUB vs Options+ HID conflict, not
  the agent.
- Cloud = **Amazon Bedrock only** (credential `AWS_BEARER_TOKEN_BEDROCK`;
  `core/cloud_backend.py` is the seam). `DA_CLOUD_PLAN` is ON and live.
- Default TTS = Kokoro local (`af_bella`, CPU); `approval_hook.py` still speaks
  via Polly directly regardless of `tts_backend`.

## 6. Process reminders (the ones that recently mattered)

- Rule 8: you're reading the session-start history check — done for you as of
  `dcffc93`.
- Rule 11 two-gate before building anything (including CG items above).
- Rule 12: decisions with meaningful rejected alternatives → `docs/decisions.md`
  (check its index for the next free D-number; D020–D022 are taken).
- Rule 13 doc matrix + run `/doc-update` at session end; pruning pass after
  every 5 merged PRs (#154–#158 just made 5).
- Eval baselines re-lock only deliberately — procedure in
  `.agents/skills/running-the-eval-harness`.

## 7. Key references

- Gap register + landscape: `docs/audits/2026-07-02-coding-agent-gap-analysis.md`
- Decisions: `docs/decisions.md` · Changelog: `docs/CHANGELOG.md`
- Architecture diagrams: `specs/ipad-sensor-focus/diagrams/00-index.md` (#16 is
  the current Python set)
- File map: `docs/file-map.md`
