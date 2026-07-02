# Session Handoff — Antigravity (2026-07-03)

**Audience:** the next Antigravity session on this repo.
**How to use:** AGENTS.md (you read it natively) is authoritative for behavior; CLAUDE.md for deep context. This document is the *delta* — exact repo state, today's work, loose ends, and operational facts that live outside the repo.

---

## 1. Repo state right now

- Branch: `master` @ `dcffc93` (all PRs merged; **no open PRs**).
- Uncommitted changes: none (workspace clean).
- `agent.db` schema: `PRAGMA user_version = 8` — unchanged.

## 2. What shipped today (2026-07-03, chronological)

| Ref | What |
|---|---|
| (none yet) | No new PRs merged today. |

## 3. CG register status (see the audit doc for full detail)

- **CG-1 egress controls — DONE** (`b268ee1`).
- **CG-2 voice rewind — DONE** (`b268ee1`).
- **CG-3 trajectory compaction‑on‑overflow — IN PROGRESS**
  - Strategy: **Summarize older entries into a compact representation** (as clarified).
- **CG-4 deny‑only local adjudicator for queued escalations — PENDING**
- **CG-5 post‑run walkthrough artifact + TTS summary — PENDING**
- **CG-6 scheduled tasks over the existing goal queue — PENDING**
- **CG-7 plan‑preview voice gate for large plans — PENDING**
- **CG-8 WorkflowRunner `pipeline` mode — PENDING**
- **CG-9 SKILL.md as the rung‑3 self‑skilling format — PENDING**

> **NOTE**: Every CG item follows the two‑gate flow (Rule 11): Draft spec → explicit human approval → `In Progress` → code.

## 4. Loose ends / landmines

1. **Spec status drift**: Verify that `specs/dev-agent-egress-controls/requirements.md` and `specs/voice-invokable-rewind/requirements.md` reflect their merged status.
2. **Branch `claude/quizzical-ptolemy-1b18b1`** still holds unmerged work (chat token auth, flag registry, etc.). Consider rebasing or merging as appropriate.
3. **Orphaned package**: `chatterbox-tts 0.1.7` remains in the venv; uninstall when convenient.
4. **Feature branch `feat/realsense-l515`** paused; ensure it does not interfere with master work.

## 5. Operational facts not derivable from the repo

- Agent runs **fully local**; iPad connects via manual IP `192.168.18.2`.
- Launcher lives at `E:\\PDA_launcher\\`; logon task has a 1‑min delay due to ReFS `E:` mount.
- Two `python.exe main.py` processes are normal (parent launcher + worker).
- Cloud backend: **Amazon Bedrock only** (`AWS_BEARER_TOKEN_BEDROCK`). `DA_CLOUD_PLAN` is ON.
- Default TTS: Kokoro local (`af_bella`), but `approval_hook.py` still uses Polly directly.

## 6. Process reminders (the ones that recently mattered)

- Rule 8: session‑start history check — done for you as of `dcffc93`.
- Rule 11: two‑gate before building anything (including CG items).
- Rule 12: decisions with meaningful rejected alternatives → `docs/decisions.md`.
- Rule 13: run `/doc-update` at session end; prune after every 5 merged PRs.
- Eval baselines re‑lock only deliberately (see `.agents/skills/running-the-eval-harness`).

## 7. Key references

- Gap register + landscape: `docs/audits/2026-07-02-coding-agent-gap-analysis.md`
- Decisions: `docs/decisions.md`
- Changelog: `docs/CHANGELOG.md`
- Architecture diagrams: `specs/ipad-sensor-focus/diagrams/00-index.md`
- File map: `docs/file-map.md`

*End of handoff.*
