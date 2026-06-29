You are running the session-end documentation hygiene checklist for the Personal Desktop Agent project. Work through every step below in order. Do not skip a step because nothing seems relevant — confirm each one explicitly.

---

## Step 1 — Current Status date

Open `CLAUDE.md`. Update the date in `## Current Status (YYYY-MM-DD)` to today. Add any PRs merged this session in the format: "Feature name (PR #NNN)". One line per PR, appended to the existing list.

**Do not add:** table counts, inline parenthetical amendments, or history. History belongs in `docs/CHANGELOG.md`.

---

## Step 2 — Feature Flags table

For each `DA_*` flag introduced or changed this session, add or update a row in the CLAUDE.md Feature Flags table:

```
| `DA_FLAG_NAME` | ON/OFF | One-line summary | D-ref or — | `specs/<feature>/` |
```

If a flag's default changed, update the existing row — do not add a duplicate.

---

## Step 3 — Decision log

For each non-obvious trade-off made this session where there was a meaningful rejected alternative: add a D-entry to `docs/decisions.md`.

Format:
```
### D0XX — <Title>
**Date:** YYYY-MM-DD
**Chose:** ...
**Rejected:** ...
**Why:** ...
**Ref:** specs/... or commit SHA
```

Add the entry to the index at the top. Keep the index under 30 lines — if at 30, prune the oldest entry fully covered by a spec.

---

## Step 4 — Gotchas

For each new invariant introduced this session: ask "Would a senior engineer, having read the spec, still be surprised by this?" If yes — add a ≤3 sentence entry to CLAUDE.md Known Gotchas with a spec ref and D-ref. If no — skip it; the content belongs in the spec.

---

## Step 5 — Pruning pass (run every session; deeper sweep every 5 merged PRs)

**Gotchas:** For each entry in CLAUDE.md Known Gotchas, check both conditions:
1. Is its behavioral content fully described in a spec under `specs/`?
2. Is any trade-off or rejected alternative captured in `docs/decisions.md`?

If both are true: remove the gotcha or compress to one sentence + spec/D-ref.

**AGENTS.md rules:** For each rule, verify all three bars:
- Phrased as MUST or MUST NOT
- Applies cross-tool (both Claude Code and Antigravity)
- Stable across features (not describing one feature's behavior)

If any bar fails: the content belongs in CLAUDE.md or a spec. Flag it in conversation for Brad to decide before removing.

**Feature Flags table:** Verify each row's spec ref still exists and the default is still accurate. Update stale rows.

---

## Step 6 — AGENTS.md gate check

If you added or changed any rule in AGENTS.md this session: confirm that change went through two-gate approval (Rule 11 — drafted in conversation, explicit approval received before committing). If not, flag it now.

---

When all six steps are complete, summarize: what was added, what was pruned, and whether any items need Brad's review before closing.
