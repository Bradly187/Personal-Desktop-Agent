# Coding-Agent Gap Analysis — PDA vs Claude Code / OpenAI Codex / Google Antigravity

**Date:** 2026-07-02
**Scope:** The DevAgent subsystem and agent harness of the Personal Desktop Agent (PDA),
compared against the July-2026 public feature sets of the three leading agentic coding
products. Accessibility/sensor pipeline is out of scope except where it changes a
recommendation. Analysis only — no specs drafted, no code changed (AGENTS.md Rule 11).
**Method:** Three parallel web-research sweeps (one per product, primary sources +
changelogs), cross-checked against PDA's actual behavior on `master` (not its docs —
two flag-table entries are known drift, see §7).

Prior related analyses: `docs/audits/2026-06-16-error-handling-gap-analysis.md`,
mini-coding-agent gap (2026-06-26), MAAD arch-design gap (2026-06-26), Claude-Code
capability-gap closure (#134–#137, 2026-06-25), vibe-coding remediation (#102–#105).

---

## 1. Landscape snapshot (July 2026)

**Claude Code** (Anthropic, v2.1.19x): plan mode + Plan subagent + cloud Ultraplan;
30-event hook system with command/HTTP/MCP/prompt/agent handlers; OS-level bash
sandbox (Seatbelt/bubblewrap, network proxy allowlist, credential deny-lists);
per-prompt checkpoints + `/rewind`; auto-compaction + microcompaction; subagents
(5-deep) → background agent fleet with auto-PR → peer agent teams → **dynamic
workflows** (model-authored JS orchestration, 16 concurrent / 1,000 agents/run,
results held outside context); auto memory (self-written per-repo MEMORY.md);
`/code-review` + cloud ultrareview + managed GitHub review fleet; **auto mode** — a
server-side classifier adjudicating every risky action with injection-resistant
context stripping; MCP tool search (deferred schemas); session portability
(teleport/Remote Control/Dispatch); Sonnet 5 (1M native) default.

**OpenAI Codex** (v0.142.x CLI + desktop app + cloud + GitHub + mobile): V4A
`apply_patch` (context-anchored hunks); sandbox modes read-only/workspace-write/full
with **network off by default** + domain-glob proxy allowlists + private-IP/DNS-rebinding
blocking + native Windows sandbox; approval policies incl. **`auto_review`** — a second
reviewer *agent* adjudicates sandbox-escape requests; model-native compaction
(trained into codex models); subagents as TOML definitions (gpt-5.4-mini as designated
subagent model); **Previews** (local best-of-N: 2–4 alternative implementations);
Goal mode (multi-day objectives); **Automations** (cron/interval, worktree-isolated);
**Memories** (`~/.codex/memories/` markdown) + Chronicle (screen-context capture,
prompt-injection caveats); **Record & Replay** (GUI demonstration → reusable skill);
hooks GA May 2026; Bedrock as a third-party model provider (Jun 2026).

**Google Antigravity** (v2.2.1; 2.0 at I/O May 2026): Agent Manager as a standalone
mission-control app (agents are the unit of work); **Artifacts** trust model (plans,
walkthroughs, screenshots, browser recordings as commentable deliverables); Inbox
(async approval/notification ledger across parallel agents); dynamic subagents +
`/schedule` cron tasks; deepest first-party **browser verification loop** (Chrome
subagent: use → observe → fix); Knowledge Base + Agent Skills (agentskills.io
standard); unified Allow/Ask/Deny permission engine with sandboxed network
allowlists — built *after* two 2025 incidents: the Turbo-mode drive wipe
(Tom's Hardware, Dec 2025) and the PromptArmor exfiltration demo (1px hidden text →
`.env` dumped to webhook.site, which was on the default URL allowlist).

Full per-product inventories with citations are preserved in the session transcript;
key URLs: code.claude.com/docs, developers.openai.com/codex/changelog,
antigravity.google/docs, promptarmor.com/resources/google-antigravity-exfiltrates-data.

---

## 2. Where PDA is ahead or genuinely differentiated

These are defensible-claim candidates for the research vision (AIOS framing):

1. **Accessibility-first multimodal control substrate.** None of the three products
   does voice/tilt/touch/gesture desktop control, let alone a 60 Hz fusion pipeline
   with priority arbitration. Codex's Record & Replay and Claude's computer-use are
   automation conveniences, not adaptive input systems.
2. **Pain-day adaptation (BehavioralTwinState).** No competitor adapts interaction
   thresholds to the user's motor condition. Unique, and load-bearing for the thesis.
3. **Fail-safe-to-DENY human gating.** PDA's voice approval gate (deliberate
   confirmation word, deny-wins-ties, silence = DENY) is a stricter posture than any
   of the three defaults. Antigravity's Turbo default ("agent decides") wiped a
   drive; PDA's D010 (rung-4 autonomous authoring forbidden) and AGENTS.md #4 are
   the principled counter-design.
4. **Inbound taint screening of tool/web output (MCPTrustClassifier).** HIGH-risk
   fetched content is withheld before it enters the reasoning context. The
   PromptArmor incident is external validation that this class of defense matters;
   none of the three ships an equivalent inbound classifier at the harness level
   (Claude Code's auto-mode probe is the closest, 2026).
5. **VRAM-governed local model roster.** Flare-aware eviction, domain specialists,
   cloud offload only for the plan domain — a resource-adaptive *local-first*
   inference layer. The big three are cloud-model-first (Codex only added Bedrock
   as a provider in Jun 2026).
6. **Eval-gated behavior with locked baselines** (31 eval files, `evals/` gates) and
   a **hash-chained audit log** — shipped as product features, not internal CI.

**Convergent-evolution validation** (PDA shipped it first or independently; industry
then shipped the same shape):

| PDA feature | Industry analog (later or parallel) |
|---|---|
| Self-skilling rung 2: experience→macros, voice-promoted (#131, Jun 22) | Codex Record & Replay (Jun 18, 2026) |
| `SKILL_QUERY`/`SKILL_CALL` progressive skill discovery | Claude Code / Codex MCP tool search (2026) |
| Saga per-step compensation (D009) | Claude Code checkpoints/rewind (Sep 2025) |
| WSL bwrap/firejail terminal routing | Codex native Windows sandbox (2026) |
| DELEGATE bounded read-only sub-investigation | Codex subagents (Mar 2026), CC Explore agent |
| Critic + Tester gates (D007/D008) | Codex `auto_review` + PR review fleets |
| Goal escalation queue + TTS announce | Antigravity Inbox, Codex Automations triage |

---

## 3. Gap register

Priorities respect PDA's constraints: single RTX 5090 (inference serializes — parallel
full agents don't parallelize), local-first, fail-safe DENY, single user with RA.

### CG-1 · Outbound egress controls on web verbs — **HIGH (security)**
- **Industry:** Codex: network off by default, domain-glob allowlists, loopback/
  private-IP blocking, DNS-rebinding checks. Claude Code: sandbox network proxy +
  per-domain approval + credential deny-lists. Antigravity: sandbox outbound
  allowlist compiled from grants (post-incident).
- **PDA today:** `RUN_TERMINAL` sandbox already gates network
  (`allow_network` off unless `command_needs_network` — good). But
  `DevAgent._fetch_url` has **no outbound restrictions**: any scheme, any IP —
  including localhost/RFC-1918 (SSRF surface: chat server :8770, Ollama :11434,
  bridge :8765 are all reachable). Inbound screening exists; outbound does not.
- **Direction:** scheme allowlist (http/https), private-IP/loopback deny,
  optional domain allowlist in config; `command_needs_network`'s auto-grant
  heuristic reviewed against the same deny-list. Small, fail-closed, no model cost.

### CG-2 · Run-level checkpoints + voice-invokable rewind — **HIGH (capability, accessibility-native)**
- **Industry:** Claude Code per-prompt checkpoints, `/rewind` restoring code and/or
  conversation, persisting across sessions.
- **PDA today:** saga snapshots exist per-write and unwind **only on failure**
  (`_halt_and_compensate`). A *successful* run that did the wrong thing has no
  one-shot undo; `DA_SAGA_GIT_BACKEND` (git-blob snapshots, no 256 KB cap) is OFF.
- **Direction:** promote saga snapshots to named per-run checkpoints; voice phrase
  "undo that run" restores. Flip `DA_SAGA_GIT_BACKEND` as the storage layer after a
  soak. For a voice-first user, spoken rewind is worth more than it is to keyboard
  users. Preserves D007/D008 non-goals (Critic pre-disk; Tester never rolls back).

### CG-3 · Trajectory compaction-on-overflow — **MEDIUM-HIGH (capability)**
- **Industry:** Claude Code auto-compaction + microcompaction; Codex compaction
  trained into the model (multi-context-window runs are routine).
- **PDA today:** `DA_TRAJECTORY_DEDUP` ON; `DA_TRAJECTORY_REDUCE` held OFF
  (~12.5 pt ordering regression). Long dev runs on 32B-class local models simply
  hit the wall.
- **Direction:** compaction is **not** reduction — it fires only near the context
  limit (where the alternative is failure), so the always-on regression trade-off
  that parked DA_TRAJECTORY_REDUCE doesn't apply. Summarize-and-continue with the
  plan + last-N steps pinned. Gate behind eval (dev_trajectory suite).

### CG-4 · Deny-only local adjudicator for queued escalations — **MEDIUM (safety/UX)**
- **Industry:** Claude Code auto mode (classifier reviews each risky action, ~30
  block rules, injection-resistant); Codex `auto_review` (reviewer agent adjudicates
  escalations).
- **PDA today:** every gated action waits on the human voice gate. Correct posture,
  but on a flare day the gate itself is a motor/energy cost.
- **Direction:** a local model pre-screens queued dev-escalations and can only
  **DENY or annotate** — never approve (fail-safe DENY invariant intact, AGENTS.md
  #4). Human approval remains the only APPROVE path; the adjudicator reduces how
  much junk reaches the human.

### CG-5 · Post-run walkthrough artifact + TTS summary — **MEDIUM (UX, accessibility-native)**
- **Industry:** Antigravity Artifacts (walkthroughs, screenshots, recordings as
  the review surface) is its core trust mechanism.
- **PDA today:** traces, live DAG, `DA_SAGA_ANNOUNCE` rollback TTS. No "what I did,
  what changed, tests" artifact after a successful run.
- **Direction:** generate a short walkthrough (files touched, diff stats, test
  outcome, screenshots for GUI-affecting steps via existing SCREENSHOT/READ_SCREEN)
  and speak a 2-sentence version. Review-by-listening instead of review-by-reading
  diffs is the accessibility translation of Antigravity's artifact model.

### CG-6 · Scheduled tasks / automations — **MEDIUM (low cost)**
- **Industry:** Codex Automations (cron, worktree-isolated, triage inbox);
  Antigravity `/schedule`; Claude Code Routines.
- **PDA today:** proactivity engine (#51) + observer agents + goal queue exist;
  no user-facing recurring schedule ("run evals nightly, TTS digest at 9am").
- **Direction:** thin scheduler over the existing goal queue; results land in the
  escalation queue + morning TTS digest. Mostly wiring, no new model or verbs.

### CG-7 · Plan-preview voice gate for large plans — **LOW-MEDIUM**
- **Industry:** Codex Plan→Pair→Execute collaboration modes; Claude Code plan mode.
- **PDA today:** per-action gating exists; there is no "speak the plan summary,
  confirm before any execution" gate for plans above N steps.
- **Direction:** for plans ≥ N steps (or containing WRITE/RUN verbs), speak a
  one-line plan summary and require the standard confirmation word. Rides the
  existing approval-gate machinery; plan-fidelity eval (#152) already measures
  plan/execution correspondence.

### CG-8 · WorkflowRunner `pipeline` mode + plan best-of-N — **LOW-MEDIUM**
- **Industry:** Claude Code dynamic workflows (deterministic orchestration code,
  results outside context); Codex Previews (2–4 alternative implementations,
  human picks).
- **PDA today:** fan-out + adversarial-verify shipped and live; `pipeline` mode
  specced, not built (specs/workflow-orchestration). MAAD analysis already flagged
  "fan-out + single-judge, not voting."
- **Direction:** build the already-specced pipeline mode; use it for best-of-N
  *plan candidates* judged locally (plan generation is cloud Sonnet via
  DA_CLOUD_PLAN — N=2–3 bounded by cost; execution stays local and serial).

### CG-9 · SKILL.md as the rung-3 self-skilling format — **LOW (strategic alignment)**
- **Industry:** agentskills.io SKILL.md is now the cross-vendor standard (Claude
  Oct 2025, Codex Dec 2025, Antigravity 2026). PDA already uses it for dev/meta
  skills in `.agents/skills/`.
- **PDA today:** runtime skills are MCP-connector JSON manifests (10); macros are
  recorded command sequences; rung 3 (parameterized skills) is specced + deferred.
- **Direction:** when rung 3 is revisited, adopt SKILL.md as its on-disk format
  instead of a bespoke one. Rung-4 prohibition (D010) unchanged.

---

## 4. Explicit non-gaps (evaluated and rejected)

| Capability | Why not |
|---|---|
| Peer agent teams / parallel full agents (CC teams, Antigravity multi-agent) | Single RTX 5090 serializes inference; `_plan_lock` exists for safety. Already reasoned in `inference/workflow.py`. Fresh-context fan-out is the right local shape. |
| 1M-token context | Structural (local 32B models). Mitigations are CG-3 + cloud plan routing (done). |
| Native Windows sandbox | WSL routing (default ON) already provides bwrap; Codex-style native sandbox is replacement, not new capability. |
| Ambient screen capture memory (Codex Chronicle) | Privacy posture + documented prompt-injection caveat; PDA has on-demand READ_SCREEN. |
| PR auto-review fleets | PDA's DevAgent writes personal code; Brad reviews with Claude Code/Antigravity. Not the product. |
| General hook system | Single user who owns the code; the extension point is the codebase itself. Revisit only if a second operator ever exists. |
| Model-native compaction / mid-task steering | Model-training capabilities, not harness features. |

---

## 5. Activation debt (cheaper than any new build)

Built gap-closures that are **dark on master**:

| Item | State | Blocker |
|---|---|---|
| `DA_REPO_CONTEXT` | OFF on master (CLAUDE.md table says ON — drift) | eval baseline lock |
| `DA_DELEGATE` | OFF on master (CLAUDE.md table says ON — drift) | eval baseline lock |
| `DA_SESSION_MEMORY` | OFF | precondition unmet (0 multi-step file-touch runs) — keep OFF |
| `DA_SAGA_GIT_BACKEND` | OFF | soak; becomes CG-2's storage layer |
| Workflow `pipeline` mode | specced, unbuilt | CG-8 |
| flags registry / doc-drift fix | uncommitted on `claude/quizzical-ptolemy-1b18b1` | needs review + merge |

Running the two eval-baseline locks and merging the flags-registry branch closes
two "gaps" without writing a line of new feature code.

---

## 6. Recommended sequence

1. **CG-1** (egress controls) — small, security-critical, no eval risk.
2. **Activation debt** — merge flags branch; run baseline locks for
   `DA_REPO_CONTEXT` / `DA_DELEGATE`.
3. **CG-2** (checkpoints + voice rewind) — highest capability-per-effort;
   accessibility-native.
4. **CG-5 + CG-6** (walkthrough TTS, schedules) — low-cost UX wins on existing
   substrate.
5. **CG-3** (compaction) — eval-gated; biggest horizon unlock for local models.
6. **CG-4, CG-7, CG-8, CG-9** — as appetite allows.

Each CG item requires a spec at `Status: Draft` + explicit approval before any code
(Rule 11). Nothing in this document authorizes implementation.
