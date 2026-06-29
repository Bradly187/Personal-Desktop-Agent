# Daily Review + Housekeeping — 2026-06-29

**Author session:** Claude Code (Opus 4.8) — automated scheduled housekeeping run
**Scope:** Review previous session's work, find stale references and problematic
code, perform housekeeping, summarize.

---

## 1. Yesterday's work (2026-06-28)

Yesterday landed two feature merges and a documentation-system foundation. Full
detail is now in [`docs/CHANGELOG.md`](../CHANGELOG.md) (catch-up entry added this
session — see §3).

| Ref | What |
|-----|------|
| **PR #149** (`627a8bc`) | Chat active-directory switching + file-context attachments |
| **PR #150** (`1ab4ec9`/`545f829`) | **Cloud plan routing** — `CloudPlanRouter` shim routes DevAgent `domain="plan"` → Sonnet 4.6 (Bedrock, forced tool-use) so the 18 GB qwen plan model stops loading. Transparent proxy; execution + fallback stay local; ContentFilter scrub + critical-forces-local; cost-ledgered. `DA_CLOUD_PLAN` (default OFF) + `DA_PLAN_WORD_TRIGGER`. 15 tests. Decision **D015**. |
| `3f3d322` | **Doc-hygiene system** — `docs/decisions.md` created (D001–D015 backfilled); AGENTS.md Rules **11** (two-gate feature approval), **12** (decision log); `/doc-update` slash command; TEMPLATE.md Status lifecycle. |
| `f947ba9` | Decision log **D016–D017**; `scripts/hooks/pre-commit` **doc-drift guard** (CLAUDE.md Gotchas ≤ 12, `docs/decisions.md` index ≤ 30). |

Context: this followed PR #147 (DevAgent saga enhancements, `0867872`) and PR #148
(`89466a4`, 06-27) — a stale-code/doc sweep that already retired `gemma3:27b` from
active code, fixed verb counts (24→16), and corrected "Danielle TTS"→Kokoro doc
strings. **That sweep is not re-done here** to avoid duplication.

---

## 2. Housekeeping findings (2026-06-29)

### 2.1 🐞 FIXED — doc-drift guard never fired (problematic code)

The doc-drift guard shipped yesterday in `f947ba9` had a logic bug that made its
`docs/decisions.md` index check a permanent no-op:

```bash
# before (broken):
INDEX_LINES=$(awk '/^---/{exit} /^- \[D/{count++} END{print count+0}' docs/decisions.md)
```

`docs/decisions.md` opens with an intro paragraph followed by a `---` horizontal
rule **before** the `## Index` block. The awk `/^---/{exit}` fired on that first
rule and exited before reaching any index row, so the counter **always returned 0**
— the 30-entry limit (the entire point of decision **D017**) was never enforced.

**Fix** — count the index rows directly; entry section headers use `### Dnnn`
(not `- [`), so the pattern matches the index block only:

```bash
# after:
INDEX_LINES=$(grep -c "^- \[D" docs/decisions.md 2>/dev/null || echo 0)
```

Verified: now counts **17** index entries correctly (limit 30 → guard passes, as it
should). The Gotchas check (9, limit 12) was already correct. Both checks simulated
green; `bash -n` clean.

### 2.2 🧹 FIXED — installed git hook had drifted from canonical

`.git/hooks/pre-commit` (the active copy) was an **older** version than the tracked
canonical `scripts/hooks/pre-commit` — it predated the bypass-flag comments
(`DA_SKIP_SECRET_SCAN` / `SKIP_DOC_GUARD`). Re-installed from the corrected canonical
copy; the two are now byte-identical. (`.git/hooks/` is untracked, so this is a
local-environment sync, not a committed change.)

### 2.3 🧹 FIXED — CHANGELOG.md was ~5 days stale

CLAUDE.md cites `docs/CHANGELOG.md` as the "Full dated history," but its newest entry
was dated **2026-06-23→24** — missing PRs **#147–#150** and the doc-hygiene system.
Added a single consolidated catch-up entry covering 06-27→28.

### 2.4 ℹ️ NOTED — residual `gemma3:27b` references (left as-is)

Remaining mentions of the retired `gemma3:27b` are in **historical** audit docs
(`docs/audits/2026-05-27-*`), planning docs (`docs/architecture/cost_savings_plan.md`),
or explicitly documented as "retired, kept pulled for rollback"
(`inference/model_router.py:66-69`) — all legitimate. Two are example judge-model
commands in `evals/README.md:60` and `evals/run.py:19`; minor and still runnable
(the model stays pulled for rollback). Flagged, not churned — below the bar for an
autonomous edit.

---

## 3. Changes made this session

| File | Change | Committed? |
|------|--------|-----------|
| `scripts/hooks/pre-commit` | Fixed decisions-index count (awk → grep); explanatory comment | tracked — pending Brad's commit |
| `.git/hooks/pre-commit` | Re-synced from canonical (local only) | n/a (untracked) |
| `docs/CHANGELOG.md` | Catch-up entry for 06-27→28 (#147–#150 + doc system) | tracked — pending Brad's commit |
| `docs/daily/2026-06-29-housekeeping-review.md` | This document | tracked — pending Brad's commit |

No schema change (`agent.db` still `PRAGMA user_version = 8` per `storage/db.py`).
No behavioral/runtime code touched — only a build-time git hook and docs. No
AGENTS.md rule additions (Rule 11 two-gate not triggered).

---

## 4. Verification

- `bash -n scripts/hooks/pre-commit` → clean
- Guard simulation: Gotchas = 9 (≤ 12), decisions index = 17 (≤ 30) → **passes correctly**
- Canonical hook == installed hook → in sync
- `git status` was clean at session start; all changes above are net-new/edits awaiting review

---

## 5. Open items for a human (not actioned autonomously)

- The fixed `scripts/hooks/pre-commit` + `docs/CHANGELOG.md` + this file are staged
  in the working tree for Brad to review and commit.
- No daily review existed for 06-27 or 06-28; this file backfills the narrative.
  Consider whether a lighter daily cadence is intended going forward.
